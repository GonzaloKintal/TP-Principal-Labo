import base64
from django.utils import timezone
from datetime import datetime, timedelta
from django.utils.timezone import now
from django.contrib.auth import get_user_model
from xmlrpc.client import NOT_WELLFORMED_ERROR
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
from licenses.utils.file_utils import *
from licenses.utils.coherence_model_ml import predict_top_3
from django.db.models import Q
from urllib.parse import unquote
import csv
from django.views.decorators.csrf import csrf_exempt


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
                request_date=datetime.now(),
                justified=False,
            )
            license_analysis(license)

            license.save()

            if not certificate_data and license.type.requieres_inmediate_certificate():
                raise Exception(f'El tipo de licencia "{license.type.name}" requiere certificado inmediato.')

            if certificate_data:
                try:
                    file_data = process_certificate(certificate_data)
                except Exception as e:
                    raise Exception(f'Error en certificado: {str(e)}')
            
            

                Certificate.objects.create(
                    license=license,
                    file=file_data,
                    validation=certificate_data.get('validation', False),
                    upload_date=datetime.now(),
                    is_deleted=False,
                    deleted_at=None
                )

            license.assign_status()

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
                        file_data = process_certificate(certificate_data)
                    except Exception as e:
                        raise Exception(f'Error en certificado: {str(e)}')
                        
                    validation = certificate_data.get('validation', False)

                    if file_data:
                        if hasattr(license, 'certificate'):
                            cert = license.certificate
                            cert.file = file_data
                            cert.validation = validation
                            cert.upload_date = datetime.now()
                            cert.is_deleted = False
                            cert.deleted_at = None
                            cert.save()
                        else:
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
                
                return JsonResponse({'message': 'Licencia actualizada exitosamente.'}, status=200)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
    


 # Aprobación de licencias
@api_view(['PUT'])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAuthenticated])
def evaluate_license(request, id):
    try:
        data = json.loads(request.body)

        license_status = data.get("license_status")
        comment = data.get("evaluation_comment", "")

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
        status_obj.save()

        # Actualizar fecha de cierre de la licencia
        license.evaluator= request.user if request.user else None
        license.closing_date = now().date()
        license.save()

        return JsonResponse({'message': f'Licencia evaluada correctamente.'}, status=200)

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
            "justified": license.justified,
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

        if not base64_string:
            return JsonResponse({"error": "El campo 'file_base64' es obligatorio"}, status=400)

        is_image=False

        if is_pdf_image(base64_string):
            is_image=True

        text= base64_to_text(base64_string,is_image)
        result = predict_top_3(text)
        parsed_result = {item[0]: item[1] for item in result}
 

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
        
        # Detectar tipo de archivo
        mime = magic.Magic(mime=True)
        file_type = mime.from_buffer(file_decoded)
        
        allowed_types = ['image/jpeg', 'image/png', 'application/pdf']
        if file_type not in allowed_types:
            raise ValueError('Tipo de archivo no permitido. Solo se aceptan JPG, PNG o PDF.')
        
        if file_type in ['image/jpeg', 'image/png']:
            file_decoded = img2pdf.convert(file_decoded)
        
        file_encoded = base64.b64encode(file_decoded).decode('utf-8')

        return file_encoded

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
