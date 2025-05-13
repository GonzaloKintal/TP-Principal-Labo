import base64
import re
import os
import pytesseract
import unicodedata

#from PIL import Image #podria no necesitarse
from pdfminer.high_level import extract_text
from datetime import datetime #podria no necesitarse
from io import BytesIO
from PyPDF2 import PdfReader
from pdf2image import convert_from_path

def is_pdf_image(base64_pdf):
   """ Determina si el PDF es una imagen"""
   pdf_bytes = base64.b64decode(base64_pdf)
   text = extract_text(BytesIO(pdf_bytes))
   return not bool(text.strip())  # True si NO hay texto

def base64_to_text(base64_pdf, is_image=False):
    """Decodifica base64 y extrae texto,le tenes que avisar avisar si pdf imagen"""
    try:
        pdf_bytes = base64.b64decode(base64_pdf)
        with open("temp.pdf", "wb") as temp_file:
            temp_file.write(pdf_bytes)
        
        if not is_image:
            with open("temp.pdf", "rb") as file:
                text = "".join([page.extract_text() for page in PdfReader(file).pages])
        else:
            text = ""
            for img in convert_from_path("temp.pdf"):
                text += pytesseract.image_to_string(img) #si aclaro el idioma para las ñ Ñ se pone raro el tesseract (img, lang='spa') podria ver el tema de la configuracion 
        return text
    
    except Exception as e:
        print(f"Error: {e}")
        return None
    finally:
        if os.path.exists("temp.pdf"):
            os.remove("temp.pdf")


def normalize_text(text): 
    """Normaliza el texto (todo minúscula, sin tildes), mantiene ñ y Ñ"""
    clean_text = []
    for char in text:
        if char in ['ñ','Ñ']:
            clean_text.append(char)
        else:
            clean_text.append(''.join(c for c in unicodedata.normalize('NFD', char) if unicodedata.category(c) != 'Mn'))
    return ''.join(clean_text).lower()

#Solo para testing
def pdf_to_base64(pdf_path):
    """Convierte un pdf a base64 y lo muestra"""
    try:
        # Leer pdf en modo binario
        with open(pdf_path, "rb") as pdf_file:
            pdf_bytes = pdf_file.read()
        
        # Codifica a base64
        base64_bytes = base64.b64encode(pdf_bytes)
        base64_text = base64_bytes.decode('utf-8')
        return base64_text

    except Exception as e:
        print(f" Error inesperado: {str(e)}")

#-------------------------------------------------
def date_in_range(certificate_text,start_date, end_date):
    """Verifica si una fecha encontrada en texto_certificado está entre licencia.start_date y licencia.end_date. """
    # Busca fechas en formato dd-mm-aaaa o dd/mm/aaaa

    match = re.search(r'(\d{2})[-/](\d{2})[-/](\d{4})',certificate_text)
    if not match:
        return False
    
    day,month, year = map(int, match.groups())
    certificate_date = datetime(year,month,day).date()
    
    return start_date <= certificate_date <= end_date

import unicodedata
import re

def normalize_text(text):
    # Convierte a minúsculas, elimina acentos y signos de puntuación
    text = text.lower()
    text = unicodedata.normalize('NFD', text)
    text = text.encode('ascii', 'ignore').decode('utf-8')  #quita acentos
    text = re.sub(r'[^\w\s]', '', text)  # quita puntuación
    return text

def search_in_pdf_text(normalized_text, search_terms):
    for term in search_terms:
        term_str = str(term).lower()
        
        if term_str.isdigit():
            #busca DNI directamente
            if not re.search(r'\b' + re.escape(term_str) + r'\b', normalized_text):
                return False
        else:
            normalized_term = normalize_text(term_str)
            if normalized_term not in normalized_text:
                return False
    return True
#Paso el base64 a texto~
#texto=base64_to_text("C:/Users/Usuario/Documents/LABORATORIO/certificados/texto_prueba.pdf",is_image=True)
#texto=base64_a_texto(texto_base64,es_imagen=True)
#print(texto)
#Los datos del empleado que voy a buscar en el pdf
#search_term = ["2020","11222333","Docente"] 

# Aviso si encontre lo que buscaba
#found = search_in_pdf_base64(texto, search_term)
#print(f"¿Se encontró '{search_term}' en el PDF? {found}")