import base64
from django.utils import timezone
from datetime import datetime, timedelta
from django.utils.timezone import now
from django.contrib.auth import get_user_model
from xmlrpc.client import NOT_WELLFORMED_ERROR
from ml_models.models import LicenseDatasetEntry
from messaging.services.messenger import MessengerService
from ml_models.anomalies.isolation_forest import get_employee_anomalies, get_supervisor_anomalies
from messaging.services.brevo_email import *
from .models import *
from django.http import JsonResponse, HttpResponse
import json
from .serializers import HealthFirstUserSerializer
from licenses.serializers import LicenseSerializer, LicenseTypeSerializer, LicenseSerializerCSV
from django.core.paginator import Paginator
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework_simplejwt.authentication import JWTAuthentication
import magic  
import img2pdf
from django.db import transaction
from .analisis import license_analysis
from ml_models.utils.file_utils import *
from ml_models.utils.coherence_model_ml import predict_license_types
from django.db.models import Q
from django.db import connection
import csv
from rest_framework.pagination import LimitOffsetPagination
from ml_models.utils.evaluation_model import predict_evaluation
import logging


logger_evaluation = logging.getLogger('licenses_evaluation')
logger_requests= logging.getLogger('licenses_requests')


# LICENSES API
@api_view(['POST'])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def licenses_list(request):
    try:
        data = json.loads(request.body)

        user_id = data.get('user_id')
        show_all_users = data.get('show_all_users', False)
        status_filter = data.get('status')
        employee_name = data.get('employee_name', '').strip()
        page_number = data.get('page', 1)
        page_size = data.get('page_size', 10)
        license_type= data.get('type', None)

        if not user_id:
            return JsonResponse({'error': 'El campo user_id es requerido.'}, status=400)

        # Obtenemos el usuario que hace la consulta
        try:
            current_user = HealthFirstUser.objects.get(id=user_id, is_deleted=False)
        except HealthFirstUser.DoesNotExist:
            return JsonResponse({'error': 'Usuario no encontradooo'}, status=404)

        queryset = License.objects.filter(is_deleted=False) # No se traen las licencias eliminadas

        # Filtro por nombre de empleado
        if employee_name:
            queryset = queryset.filter(
            Q(user__first_name__icontains=employee_name) | 
            Q(user__last_name__icontains=employee_name)
        )

        if license_type:
            queryset = queryset.filter(type__name=license_type)

        # Filtro por estado
        if status_filter:
            status_filter = status_filter.lower()
            if status_filter == "approved":
                queryset = queryset.filter(status__name=Status.StatusChoices.APPROVED)
            elif status_filter == "pending":
                queryset = queryset.filter(status__name=Status.StatusChoices.PENDING)
            elif status_filter == "rejected":
                queryset = queryset.filter(status__name=Status.StatusChoices.REJECTED)
            elif status_filter == "expired":
                queryset = queryset.filter(status__name=Status.StatusChoices.EXPIRED)
            elif status_filter == "missing_doc":
                queryset = queryset.filter(status__name=Status.StatusChoices.MISSING_DOC)

        role_name = current_user.role.name if current_user.role else None

        if role_name in ['employee', 'analyst']:
            if role_name in ['employee', 'analyst']:
                # Solo sus licencias (usa user_id como filtro principal)
                queryset = queryset.filter(user__id=user_id)

        elif role_name in ['admin', 'supervisor']:
            if not show_all_users:
                queryset = queryset.filter(user=current_user)
            # Si show_all_users es True, vemos licencias de todos los usuarios
        else:
            queryset = queryset.none()

        queryset = queryset.order_by('-start_date')

        paginator = Paginator(queryset, page_size)
        page = paginator.get_page(page_number)

        serializer = LicenseSerializer(page.object_list, many=True)

        return JsonResponse({
            'licenses': serializer.data,
            'total_pages': paginator.num_pages,
            'current_page': page.number,
            'total_licenses': paginator.count,
        }, status=200)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@api_view(['POST'])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def create_license(request):
    try:
        data = json.loads(request.body)

        user_id = data.get('user_id')
        license_type_id = data.get('type_id')
        start_date = data.get('start_date')
        end_date = data.get('end_date')
        information = data.get('information', '')
        certificate_data = data.get('certificate', None)

        if not all([user_id, license_type_id, start_date, end_date]):
            return JsonResponse({'error': 'user_id, type, start_date, end_date son requeridos.'}, status=400)

        try:
            license_type = LicenseType.objects.get(id=license_type_id)
        except LicenseType.DoesNotExist:
                return JsonResponse({'error': f'LicenseType con id "{license_type_id}" no encontrado.'}, status=404)

        User = get_user_model()
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return JsonResponse({'error': 'Usuario no encontrado.'}, status=404)

        start_date_parsed = datetime.strptime(start_date, '%Y-%m-%d').date()
        end_date_parsed = datetime.strptime(end_date, '%Y-%m-%d').date()

        if end_date_parsed < start_date_parsed:
            return JsonResponse({'error': 'end_date no puede ser anterior a start_date.'}, status=400)

        required_days = (end_date_parsed - start_date_parsed).days + 1

        with transaction.atomic():
            license = License(
                user=user,
                type=license_type,
                start_date=start_date_parsed,
                end_date=end_date_parsed,
                required_days=required_days,
                information=information,
                request_date=datetime.now()
            )
            license_analysis(license)

            license.save()

            if not certificate_data and license.type.requieres_inmediate_certificate():
                raise Exception(f'El tipo de licencia "{license.type.name}" requiere certificado inmediato.')

            if certificate_data:
                try:
                    file_data, certificate_obj = process_certificate(certificate_data)
                except Exception as e:
                    raise Exception(f'Error en certificado: {str(e)}')

                validation = certificate_data.get('validation', False)

                if certificate_obj:
                    # Certificado HFCOD ya existente y válido
                    certificate_obj.license = license
                    certificate_obj.file = file_data
                    certificate_obj.validation = validation
                    certificate_obj.upload_date = datetime.now()
                    certificate_obj.is_deleted = False
                    certificate_obj.deleted_at = None
                    certificate_obj.save()
                else:
                    # Certificado nuevo sin HFCOD
                    Certificate.objects.create(
                        license=license,
                        certificate_id=None,
                        file=file_data,
                        validation=validation,
                        upload_date=datetime.now(),
                        is_deleted=False,
                        deleted_at=None
                    )

            license.assign_status()
            #if license.type and license.type.certificate_require and certificate_data is None:
            #    MessengerService.send_upload_license_without_certificate_message(license)
            #else:
            #    MessengerService.send_upload_license_message(license)
        logger_requests.info(f'Licencia con id  {license.license_id} solicitada por {request.user.first_name} {request.user.last_name} con id: {request.user.id}')

        return JsonResponse({'message': 'Licencia solicitada exitosamente.'}, status=200)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)



@api_view(['DELETE'])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def delete_license(request, id):
    if not id:
        return JsonResponse({'error': 'El ID es requerido.'}, status=400)

    try:
        license_obj = License.objects.get(license_id=id, is_deleted=False)
        license_obj.is_deleted = True
        license_obj.deleted_at = timezone.now()
        license_obj.save()
        return JsonResponse({'message': 'Licencia eliminada correctamente.'}, status=200)

    except License.DoesNotExist:
        return JsonResponse({'error': 'La licencia no existe.'}, status=404)
    

@api_view(['PUT'])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def update_license(request, id):
    try:
        data = json.loads(request.body)

        license_type_id = data.get('type_id')
        start_date = data.get('start_date')
        end_date = data.get('end_date')
        information = data.get('information', '')
        certificate_data = data.get('certificate', None)

        try:
            license = License.objects.get(pk=id, is_deleted=False)
        except License.DoesNotExist:
            return JsonResponse({'error': f'Licencia con id "{id}" no encontrada.'}, status=404)

        # Validar y actualizar tipo de licencia
        if license_type_id:
            try:
                license_type = LicenseType.objects.get(id=license_type_id)
                license.type = license_type
            except LicenseType.DoesNotExist:
                return JsonResponse({'error': f'Tipo de licencia con id "{license_type_id}" no encontrado.'}, status=404)

        # Validar y actualizar fechas
        if start_date and end_date:
            start_date_parsed = datetime.strptime(start_date, '%Y-%m-%d').date()
            end_date_parsed = datetime.strptime(end_date, '%Y-%m-%d').date()
            if end_date_parsed < start_date_parsed:
                return JsonResponse({'error': 'end_date no puede ser anterior a start_date.'}, status=400)
            license.start_date = start_date_parsed
            license.end_date = end_date_parsed
            license.required_days = (end_date_parsed - start_date_parsed).days + 1

        with transaction.atomic():
                license.information = information
                license.save()
                license_analysis(license)

                # Actualizar certificado
                if certificate_data:
                    try:
                        file_data, certificate_obj = process_certificate_update_certificate(certificate_data, license)
                    except Exception as e:
                        raise Exception(f'Error en certificado: {str(e)}')
                        
                    validation = certificate_data.get('validation', False)

                    if file_data:
                        if certificate_obj:
                            # Reutilizar el certificado ya existente
                            cert = certificate_obj
                            cert.license = license  # Asegurar que esté vinculado correctamente
                            cert.file = file_data
                            cert.validation = validation
                            cert.upload_date = datetime.now()
                            cert.is_deleted = False
                            cert.deleted_at = None
                            cert.save()
                        elif license.certificate:
                            # Si ya tiene uno que no es HFCOD, se actualiza
                            cert = license.certificate
                            cert.file = file_data
                            cert.validation = validation
                            cert.upload_date = datetime.now()
                            cert.is_deleted = False
                            cert.deleted_at = None
                            cert.save()
                        else:
                            # Si no hay ninguno, se crea
                            Certificate.objects.create(
                                license=license,
                                file=file_data,
                                validation=validation,
                                upload_date=datetime.now(),
                                is_deleted=False,
                                deleted_at=None
                            )
                if license.status.name not in [Status.StatusChoices.APPROVED, Status.StatusChoices.REJECTED]:
                    try:
                        certificate=license.certificate
                    except Certificate.DoesNotExist:
                        certificate=None

                    if  not license.type.certificate_require:
                        license.status.name = Status.StatusChoices.PENDING
                        if certificate is not None:
                            license.certificate.delete()
                    elif license.type.certificate_require and certificate is None:
                        license.status.name = Status.StatusChoices.MISSING_DOC
                    else:
                        license.status.name = Status.StatusChoices.PENDING
                    
                    license.status.save()

                logger_requests.info(f"Usuario {request.user.first_name} {request.user.last_name} con id {request.user.id} edito la licencia {id}") 
                return JsonResponse({'message': 'Licencia actualizada exitosamente.'}, status=200)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@api_view(['PUT'])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def add_certificate(request, id):
    status_code = 200
    response_data = {}

    try:
        data = json.loads(request.body)
        certificate_data = data.get('certificate', None)

        # Validar que la LICENCIA a editar EXISTE
        try:
            license = License.objects.get(license_id=id, is_deleted=False)
        except License.DoesNotExist:
            raise Exception(f'Licencia con id "{id}" no encontrada.')

        if not license.type.certificate_require:
            raise Exception('El tipo de licencia no requiere certificado.')

        try:
            file_data, certificate_obj = process_certificate_add_certificate(certificate_data)
        except Exception as e:
            raise Exception(f'Error en certificado: {str(e)}')

        # Asociar y guardar el certificado existente
        certificate_obj.license = license
        certificate_obj.file = file_data
        certificate_obj.validation = False
        certificate_obj.upload_date = datetime.now()
        certificate_obj.is_deleted = False
        certificate_obj.deleted_at = None
        certificate_obj.save()

        # Cambiar estado de la licencia
        license.status.name = Status.StatusChoices.PENDING
        license.status.save()

        response_data = {'message': 'Certificado agregado exitosamente.'}

    except Exception as e:
        status_code = 500
        response_data = {'error': str(e)}

    return JsonResponse(response_data, status=status_code)
        


    
    


 # Aprobación de licencias
@api_view(['PUT'])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def evaluate_license(request, id):
    try:
        logger_evaluation.info(f"Evaluando licencia {id}")
        data = json.loads(request.body)
        license_status = data.get("license_status")
        comment = data.get("evaluation_comment", "")
        other_comment = data.get("other_evaluation_comment", "")

        if license_status not in ["approved", "rejected", "missing_doc"]:
            return JsonResponse({'error': 'Estado inválido. Debe ser "approved" o "rejected".'}, status=400)

        # Verificar existencia de la licencia
        try:
            license = License.objects.get(license_id=id)
        except License.DoesNotExist:
            return JsonResponse({'error': 'Licencia no encontrada.'}, status=404)

        # Obtener o crear el objeto Status
        status_obj, created = Status.objects.get_or_create(license=license)

        # Solo permitir evaluación si el estado actual es 'pending'
        if not created and status_obj.name != Status.StatusChoices.PENDING:
            return JsonResponse({
                'error': f'La licencia no pudo ser evaluada. Estado actual: "{status_obj.name}".'
            }, status=400)

        # Actualizar estado, fecha y comentario
        status_obj.name = license_status
        status_obj.evaluation_date = now().date()
        status_obj.evaluation_comment = comment
        status_obj.other_evaluation_comment= other_comment
        status_obj.save()

        # Actualizar fecha de cierre de la licencia
        license.evaluator= request.user if request.user else None
        license.closing_date = now().date()
        license.save()

        if license_status == Status.StatusChoices.REJECTED:
            MessengerService.send_rejected_license_message(license)
        if license_status == Status.StatusChoices.APPROVED:
            MessengerService.send_approved_license_message(license)

        evaluator= f"{request.user.first_name} {request.user.last_name}"

        if license.type.certificate_require:
            base64_certificate = license.certificate.file
            is_image=False

            if is_pdf_image(base64_certificate):
                is_image=True

            text= base64_to_text(base64_certificate,is_image)
            text_normalize=normalize_text(text)
            if comment!='Otro':
                LicenseDatasetEntry.objects.create(
                    text=text_normalize,
                    type=license.type.group,
                    status=license.status.name,
                    reason=license.status.evaluation_comment.lower()
                )
        logger_evaluation.info(f"Licencia  {id} evaluada correctamente. estado: {license_status}, comentario: {comment}, evaluador: {evaluator} id: {license.evaluator.id}")


        return JsonResponse({'message': 'Licencia evaluada correctamente.','evaluator': evaluator}, status=200)


    except json.JSONDecodeError:
        return JsonResponse({'error': 'El cuerpo de la solicitud debe ser JSON válido.'}, status=400)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# Detalle de licencia
@api_view(['GET'])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def get_license_detail(request, id):
    try:
        #User = get_user_model()

        try:
            license = License.objects.select_related("user", "status").get(license_id=id)
        except License.DoesNotExist:
            return JsonResponse({"error": "Licencia no encontrada."}, status=404)

        user = request.user

        # Validación de permisos. Descomentar cuando se implemente token.
        #allowed_roles = ["supervisor", "admin"]
        #if (license.user != user and user.role not in allowed_roles):
        #    return JsonResponse({"error": "No tenés permisos para acceder a esta licencia."}, status=403)

        # Datos del usuario solicitante
        user_data = HealthFirstUserSerializer(license.user).data

        # Datos de la licencia
        license_data = {
            "type": license.type.name,
            "start_date": license.start_date,
            "end_date": license.end_date,
            "request_date": license.request_date,
            "closing_date": license.closing_date,
            "required_days": (license.end_date - license.start_date).days + 1,
            "information": license.information,
            "evaluator": (license.evaluator.first_name + ' ' + license.evaluator.last_name) if license.evaluator else "",
            "evaluator_role": license.evaluator.role.name if license.evaluator else "",
        }

        # Estado actual de la licencia
        status_data = None
        if hasattr(license, "status"):
            status_data = {
                "name": license.status.name,
                "evaluation_date": license.status.evaluation_date,
                "evaluation_comment": license.status.evaluation_comment,
                "other_evaluation_comment": license.status.other_evaluation_comment if license.status.other_evaluation_comment else "",
            }

        # Certificado relacionado
        certificate = Certificate.objects.filter(license=license, is_deleted=False).first()
        certificate_data = None
        if certificate:
            certificate_data = {
                "validation": certificate.validation,
                "upload_date": certificate.upload_date,
                "file": certificate.file
            }

        return JsonResponse({
            "license": license_data,
            "user": user_data,
            "status": status_data,
            "certificate": certificate_data
        }, status=200)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@api_view(['GET'])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def get_licenses_types(request):
    try:
        types = LicenseType.objects.filter(is_deleted=False)
        types_data = LicenseTypeSerializer(types, many=True).data
        return JsonResponse({"types": types_data}, status=200)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@api_view(['POST'])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def upload_base64_file(request):

    try:
        body_unicode = request.body.decode('utf-8')
        body = json.loads(body_unicode)
        base64_string = body.get("file_base64")
        license_id=body.get("license_id", None)

        if not license_id:
            return JsonResponse({"error": "El campo 'license_id' es obligatorio"}, status=400)
        if not base64_string:
            return JsonResponse({"error": "El campo 'file_base64' es obligatorio"}, status=400)
        license=License.objects.get(license_id=license_id)
        is_image=False

        if is_pdf_image(base64_string):
            is_image=True

        text= base64_to_text(base64_string,is_image)
        license_type_prediction = predict_license_types(text)
        evaluation_prediction = predict_evaluation(text,license.type.group)

        result = {
            "is_approved": bool(evaluation_prediction["approved"]),
            "probability_of_approval": evaluation_prediction["probability_of_approval"],
            "probability_of_rejection": evaluation_prediction["probability_of_rejection"],
            "reason_of_rejection": evaluation_prediction["reason_of_rejection"] if evaluation_prediction["reason_of_rejection"] else "",
            "top_reasons": evaluation_prediction["top_reasons"] if "top_reasons" in evaluation_prediction else "",
            "license_types": license_type_prediction,
        }
        if license.type.group=='enfermedad':
            result["has_code"] = evaluation_prediction["has_code"]

        parsed_result = json.loads(json.dumps(result))
 

        if "error" in result:
            return JsonResponse(parsed_result, status=500)

        return JsonResponse(parsed_result, status=200)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)



def process_certificate(certificate_data):
        file_data = certificate_data.get('file', None)
        if not file_data:
            raise ValueError('Archivo del certificado no encontrado.')
        
        file_decoded = base64.b64decode(file_data)

        """ Validacíon de código unico """
        certificate_id = extract_certificate_id_from_pdf_base64(file_data)
        print("El codigo de certficiado es: ", certificate_id)
        """ FIN Validación de codigo unico """
        
        certificate_obj = None

        # Detectar tipo de archivo
        mime = magic.Magic(mime=True)
        file_type = mime.from_buffer(file_decoded)
        
        allowed_types = ['image/jpeg', 'image/png', 'application/pdf']
        if file_type not in allowed_types:
            raise ValueError('Tipo de archivo no permitido. Solo se aceptan JPG, PNG o PDF.')
        
        if file_type in ['image/jpeg', 'image/png']:
            file_decoded = img2pdf.convert(file_decoded)


        # Si encontro certificate_id significa que es HFCOD, debe existir el certificado en la BD:
        if certificate_id: 
            try:
                certificate_obj = Certificate.objects.get(certificate_id=certificate_id)
            except Certificate.DoesNotExist:
                raise ValueError("El código de certificado no existe en la dB.")

            # Validar que el CERTIFICADO NO ESTE relacionado a la LICENCIA.
            if certificate_obj.license is not None:
                raise ValueError("El certificado ya fue utilizado.")
        
        file_encoded = base64.b64encode(file_decoded).decode('utf-8')

        return file_encoded, certificate_obj




def process_certificate_add_certificate(certificate_data):
        file_data = certificate_data.get('file', None)
        if not file_data:
            raise ValueError('Archivo del certificado no encontrado.')
        
        file_decoded = base64.b64decode(file_data)

        """ Validacíon de código unico """
        certificate_id = extract_certificate_id_from_pdf_base64(file_data)
        print("El codigo de certficiado es: ", certificate_id)
        """ FIN Validación de codigo unico """
        
        # Detectar tipo de archivo
        mime = magic.Magic(mime=True)
        file_type = mime.from_buffer(file_decoded)
        
        allowed_types = ['image/jpeg', 'image/png', 'application/pdf']
        if file_type not in allowed_types:
            raise ValueError('Tipo de archivo no permitido. Solo se aceptan JPG, PNG o PDF.')
        
        #if file_type in ['image/jpeg', 'image/png']:
        #    file_decoded = img2pdf.convert(file_decoded)


        # Si el código existe
        if certificate_id:
            try:
                certificate_obj = Certificate.objects.get(certificate_id=certificate_id)
                print(f"Usando certificado existente con ID {certificate_obj.certificate_id}")    
            except Certificate.DoesNotExist:
                raise ValueError(f"El certificado con ID {certificate_id} no existe.")

            # Validar que el código del certificado no esté relacionado con otra licencia
            if certificate_obj.license is not None:
                raise ValueError("El certificado ya fue utilizado.")
        else:
            # Si no tiene el prefijo HFCOD, simplemente generamos un nuevo certificado "genérico"
            certificate_obj = Certificate()

        # Codificamos nuevamente el archivo para guardar
        file_encoded = base64.b64encode(file_decoded).decode('utf-8')
        return file_encoded, certificate_obj


def process_certificate_update_certificate(certificate_data, current_license):
        file_data = certificate_data.get('file', None)
        if not file_data:
            raise ValueError('Archivo del certificado no encontrado.')
        
        file_decoded = base64.b64decode(file_data)

        """ Validacíon de código unico """
        certificate_id = extract_certificate_id_from_pdf_base64(file_data)
        print("El codigo de certficiado es: ", certificate_id)
        certificate_obj = None
        """ FIN Validación de codigo unico """
        
        
        # Detectar tipo de archivo
        mime = magic.Magic(mime=True)
        file_type = mime.from_buffer(file_decoded)
        
        allowed_types = ['image/jpeg', 'image/png', 'application/pdf']
        if file_type not in allowed_types:
            raise ValueError('Tipo de archivo no permitido. Solo se aceptan JPG, PNG o PDF.')
        
        if file_type in ['image/jpeg', 'image/png']:
            file_decoded = img2pdf.convert(file_decoded)


         # Buscar el certificado en la base de datos
        if certificate_id:
            try:
                certificate_obj = Certificate.objects.get(certificate_id=certificate_id)
            except Certificate.DoesNotExist:
                raise ValueError("El código de certificado no existe en el sistema.")

            # Validar si está asignado a otra licencia
            if certificate_obj.license and certificate_obj.license.pk != current_license.pk:
                raise ValueError("Este certificado ya está asignado a otra licencia.")

            # Si la licencia ya tiene un certificado, se valida que sea el mismo código
            certificate = getattr(current_license, 'certificate', None)
            if certificate:
                current_certificate_id = str(current_license.certificate.certificate_id)
                if str(certificate_id) != current_certificate_id:
                    raise ValueError("No se puede reemplazar el certificado: el código no coincide con el actual.")
            else:
                raise ValueError("La licencia no posee un certificado actual para validar el código.")
        else:
            # No tiene HFCOD entonces no se valida el código, se permite continuar
            certificate_obj = None    
        
        file_encoded = base64.b64encode(file_decoded).decode('utf-8')

        return file_encoded, certificate_obj


@api_view(['POST'])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def export_licenses_to_csv(request):
    try:
        data = json.loads(request.body)
        user_id = data.get('user_id')
        show_all_users = data.get('show_all_users', False)
        status_filter = data.get('status')
        employee_name = data.get('employee_name', '').strip()


        if not user_id:
            return JsonResponse({'error': 'El campo user_id es requerido.'}, status=400)

        try:
            current_user = HealthFirstUser.objects.get(id=user_id, is_deleted=False)
        except HealthFirstUser.DoesNotExist:
            return JsonResponse({'error': 'Usuario no encontradooo'}, status=404)

        queryset = License.objects.filter(is_deleted=False) 

        if employee_name:
            queryset = queryset.filter(
            Q(user__first_name__icontains=employee_name) | 
            Q(user__last_name__icontains=employee_name)
        )

        # Filtro por estado
        if status_filter:
            queryset = queryset.filter(status__name=status_filter)

        role_name = current_user.role.name if current_user.role else None

        if role_name in ['employee', 'analyst']:
            if role_name in ['employee', 'analyst']:
                queryset = queryset.filter(user__id=user_id)

        elif role_name in ['admin', 'supervisor']:
            if not show_all_users:
                queryset = queryset.filter(user=current_user)
        else:
            queryset = queryset.none()


        licenses_data = LicenseSerializerCSV(queryset, many=True).data

        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="licenses.csv"'

        writer = csv.writer(response)
        if licenses_data:
            column_headers = {
                'id': 'ID de Licencia',
                'username': 'Nombre de Usuario',
                'user_name': 'Nombre Completo',
                'type': 'Tipo de Licencia',
                'start_date': 'Fecha de Inicio',
                'end_date': 'Fecha de Fin',
                'days': 'Días',
                'status': 'Estado',
                'information': 'Información',
                'evaluator': 'Evaluador',
            }
            original_fields = LicenseSerializerCSV.Meta.fields

            headers = [column_headers.get(field, field) for field in original_fields]
            writer.writerow(headers)
            for item in licenses_data:
                writer.writerow([item.get(field, '') for field in original_fields])
        else:
            writer.writerow(['No data found'])

        return response

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
        


##################### API Anomalias #########################
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
@api_view(['GET'])
def supervisor_anomalies(request):
    start_date = request.GET.get('start_date') or None
    end_date = request.GET.get('end_date') or None
    evaluator_id = request.GET.get('user_id') or None
    is_anomaly = request.GET.get('is_anomaly') or None

    try:
        df = get_supervisor_anomalies(start_date, end_date)

        if evaluator_id:
            df = df[df['evaluator_id'] == int(evaluator_id)]

        if is_anomaly is not None:
            if is_anomaly.lower() in ['true', '1', 'yes']:
                anomaly_flag = True
            elif is_anomaly.lower() in ['false', '0', 'no']:
                anomaly_flag = False
            else:
                anomaly_flag = None

            if anomaly_flag is not None:
                df = df[df['is_anomaly'] == anomaly_flag]

        data = df.to_dict(orient='records')

        # Aplicar paginación
        paginator = LimitOffsetPagination()
        paginated_data = paginator.paginate_queryset(data, request)

        return paginator.get_paginated_response(paginated_data)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
    

@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
@api_view(['GET'])
def employee_anomalies(request):
    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')
    employee_id = request.GET.get('employee_id')
    is_anomaly = request.GET.get('is_anomaly')

    try:
        df = get_employee_anomalies(start_date, end_date)

        # Filtro para excluir registros sin solicitudes
        df = df[df['total_requests'] > 0]

        if employee_id:
            df = df[df['employee_id'] == int(employee_id)]

        if is_anomaly is not None:
            anomaly_flag = is_anomaly.lower() in ['true', '1', 'yes']
            df = df[df['is_anomaly'] == int(anomaly_flag)]

        data = df.to_dict(orient='records')

        # Aplicar paginación
        paginator = LimitOffsetPagination()
        paginated_data = paginator.paginate_queryset(data, request)

        return paginator.get_paginated_response(paginated_data)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)





def get_next_certificate_id():
    with connection.cursor() as cursor:
        cursor.execute("SELECT nextval(pg_get_serial_sequence('licenses_certificate', 'certificate_id'))")
        row = cursor.fetchone()
    return row[0] if row else None

@api_view(['GET'])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def generate_certificate_code(request):
    try:
        # Obtener proximo id de tabla certificate
        next_id = get_next_certificate_id()
        if not next_id:
            return JsonResponse({'error': 'No se pudo obtener el próximo ID de certificado.'}, status=500)

        # Crear el codigo
        code = f"HFCOD{next_id}"

        # Crear el Certificate sin licencia relacionada
        Certificate.objects.create(
            certificate_id=next_id,
            license=None,
            file=None,
            validation=False,
            upload_date=datetime.now(),
            is_deleted=False,
            deleted_at=None
        )

        template_path = os.path.join(settings.BASE_DIR, 'public', 'templates', 'standard_format.pdf')

        modified_pdf = insert_code_to_pdf_return_bytes(template_path, code)

        response = HttpResponse(modified_pdf, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="Formato_Certificado_HealthFirst.pdf"'
        return response

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)