import pandas as pd
import joblib

from pathlib import Path
from functools import lru_cache
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.ensemble import RandomForestClassifier
from sklearn.pipeline import make_pipeline
from file_utils import super_clean
#from .file_utils import super_clean

# Paths
MODEL_PATH = Path(__file__).resolve().parent / 'modelo_clasificador.joblib'
DATASET_PATH = Path(__file__).resolve().parent / 'coherence_license_type_dataset.csv'


def load_data(csv_file: Path):
    """Loads and normalizes the dataset from a CSV file."""
    df = pd.read_csv(
        csv_file,
        delimiter=',',
        quotechar='"',
        engine='python',
        on_bad_lines='skip'
    )
    df['text'] = df['text'].apply(super_clean)
    return df['text'].tolist(), df['clase'].tolist()


def train_and_save_model():
    texts, labels = load_data(DATASET_PATH)

    model = make_pipeline(
        TfidfVectorizer(
                max_features=5000,
                ngram_range=(1, 2),
                min_df=2,     
        ),
        RandomForestClassifier(
            n_estimators=500,
            class_weight='balanced',    
        )
    )
    model.fit(texts, labels)
    joblib.dump(model, MODEL_PATH)
    return model


# @lru_cache(maxsize=1)
def get_model():
    if MODEL_PATH.exists():
        return joblib.load(MODEL_PATH)
    else:
        return train_and_save_model()


def predict_top_3(text):
    model = get_model()
    normalized_text = super_clean(text)
    probabilities = model.predict_proba([normalized_text])[0]

    results = [
        (str(label), f"{prob * 100:.1f}%")
        for label, prob in zip(model.classes_, probabilities)
    ]

    return sorted(results, key=lambda x: float(x[1][:-1]), reverse=True)[:3]


if __name__ == "__main__":
    # 1. Inicializar modelo
    #Para probar
    texto="El de Guaymallén deja constancia de que Carla Daniela López," \
    "DNI 30.987.654, de 31 años, asistió a control prenatal el día 10/08/2024 en el Hospital" \
    "Italiano de Buenos Aires, certificándose que cursa la semana 24 de gestación con Fecha" \
    "Probable de Parto para el 10/11/2024. Se prevé que el nacimiento del niño, que será inscripto" \
    "bajo el apellido López, ocurra sin complicaciones según diagnóstico médico. Además, se indica" \
    "suplementación con hierro y ácido fólico, y se informa que la inscripción del acta de nacimiento" \
    "deberá efectuarse  a lo establecido en el libro correspondiente de este . Dra. Ana López."
    print(predict_top_3(texto))