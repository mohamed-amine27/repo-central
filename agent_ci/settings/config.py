import os
SONARQUBE_URL   = "https://sonarcloud.io"
SONAR_TOKEN = "caa8b76e83b76459b1296b4d009d9330d07f4d88"
SONARQUBE_ORGANIZATION = "mohamed-amine27"
MAX_ISSUES      = 30  
import os

# Utilise la variable d'env CI si définie, sinon fallback local Windows
SONAR_SCANNER_CMD = os.environ.get(
    "SONAR_SCANNER_CMD",
    r"C:\Users\moham\Downloads\node-v23.7.0-win-x64\sonar-scanner.cmd"
)
MODEL_NAME = "Meta-Llama-3.3-70B-Instruct" 
SAMBANOVA_API_KEY = "bd52c437-dc67-4a00-8103-127a96578fae"
TEMPERATURE  = 0.0