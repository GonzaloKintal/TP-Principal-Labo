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
from nltk.stem import SnowballStemmer #NUEVO
from nltk.corpus import stopwords #NUEVO

#nltk>=3.6.2
#pip install nltk
#python -c "import nltk; nltk.download('stopwords')"


def is_pdf_image(base64_pdf):
   """ Determina si el PDF es una imagen"""
   pdf_bytes = base64.b64decode(base64_pdf)
   text = extract_text(BytesIO(pdf_bytes))
   return not bool(text.strip())  # True si NO hay texto

def normalize_text(text):
    """Normaliza texto: minúsculas, sin tildes, sin puntuación, conserva ñ/Ñ"""
    if not isinstance(text, str):
        return ""  # Maneja valores no-string
    
    clean_text = []
    for char in text:
        if char in ['ñ', 'Ñ']:
            clean_text.append(char)
        else:
            normalized_char = unicodedata.normalize('NFD', char)
            clean_text.append(''.join(c for c in normalized_char if unicodedata.category(c) != 'Mn'))
    text = ''.join(clean_text).lower()
    text = re.sub(r'[^\wñÑ\s]', '', text)  # Remueve puntuación pero conserva espacios
    text = re.sub(r'\s+', ' ', text).strip()
    return text

stopwords_es = set(stopwords.words('spanish'))  # Convertir a set
extra_stopwords = {'certifica', 'certifico', 'fecha', 'firma', 'sello', 'certificado', 'matricula', 'hora','horas'} 
stopwords_es.update(extra_stopwords)  

stemmer = SnowballStemmer('spanish')

def super_clean(text):
    text = normalize_text(text)  # Primero aplica  normalización
    words = [
        stemmer.stem(w) 
        for w in text.split() 
        if (w not in stopwords_es and len(w) > 2)  # Filtra stopwords y palabras cortas
    ]
    return ' '.join(words)

def base64_to_text(base64_pdf, is_image=False):
    """Decodifica un PDF en base64 y extrae texto. Usa OCR si is_image=True."""
    try:
        pdf_bytes = base64.b64decode(base64_pdf)
        with open("temp.pdf", "wb") as temp_file:
            temp_file.write(pdf_bytes)

        text = ""

        if not is_image:
            with open("temp.pdf", "rb") as file:
                reader = PdfReader(file)
                for page in reader.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text += page_text
        else:
            images = convert_from_path("temp.pdf")
            for img in images:
                text += pytesseract.image_to_string(img, lang='spa')  # si tenés soporte

        return text.strip()

    except Exception as e:
        print(f"Error: {e}")
        return None
    finally:
        if os.path.exists("temp.pdf"):
            os.remove("temp.pdf")

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

#vamo a ver si sirve
def date_in_range(certificate_text,license):
    """Verifica si una fecha encontrada en texto_certificado está entre licencia.start_date y licencia.end_date. """
    # Busca fechas en formato dd-mm-aaaa o dd/mm/aaaa

    match = re.search(r'(\d{2})[-/](\d{2})[-/](\d{4})',certificate_text)
    if not match:
        return False
    
    day,month, year = map(int, match.groups())
    certificate_date = datetime(year,month,day).date()
    
    return license.start_date <= certificate_date <= license.end_date

#vamo a ver si sirve
def search_dni_in_text(text, dni):
    """Busca un DNI (sin puntos) en un texto que puede tenerlo con/sin puntos/espacios.  """
    # Elimina puntos y espacios del texto para normalizarlo
    text_clean = re.sub(r'[.\s]', '', text)
    # Busca el DNI (sin puntos) como palabra completa (\b = límite de palabra)
    pattern = r'\b{}\b'.format(re.escape(str(dni)))
    return re.search(pattern, text_clean) is not None

#vamo a ver si sirve
def search_in_pdf_text(text, search_terms):
    """Busca términos en el texto, manejando DNIs en el texto con/sin puntos"""
    text_lower = text.lower()  # Texto del certificado en minúsculas
    
    for term in search_terms:
        term_str = str(term).lower()
        
        # Caso especial para DNIs (numéricos sin puntos)
        if term_str.isdigit():  # Asume que el DNI en search_terms NO tiene puntos
            if not search_dni_in_text(text_lower, term_str):  # Busca el DNI limpio
                return False
        else:
            # Búsqueda normal para nombres/apellidos
            if term_str not in text_lower:
                return False
    return True
