
"""
OBJETIVO:
Crear respaldos versionados del archivo pedidos.json
que se encuentra en el almacenamiento persistente EFS.

FUNCIONAMIENTO:
1. Comprueba que el archivo de datos existe.
2. Lee y valida su contenido JSON.
3. Obtiene la versión de los datos.
4. Calcula el hash SHA-256 del archivo.
5. Genera un nombre único para el respaldo.
6. Envía el archivo a Amazon S3.
7. Registra el resultado de la operación.

IMPORTANTE:
- No modifica el archivo original.
- No elimina respaldos anteriores.
- El bucket S3 debe existir previamente.
- Requiere permisos IAM para escribir en S3.
"""

# ======================================================
# 1. IMPORTACIÓN DE LIBRERÍAS
# ======================================================

import os
import json
import hashlib
import logging

from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError


# ======================================================
# 2. CONFIGURACIÓN
# ======================================================

# Ruta del archivo que queremos respaldar.
#
# Por defecto, el archivo está en /data/pedidos.json,
# que será una carpeta montada desde Amazon EFS.
#
# Podemos cambiarla mediante la variable DATA_FILE
# para ejecutar pruebas en otro entorno.

DATA_FILE = Path(
    os.getenv("DATA_FILE", "/data/pedidos.json")
)

# Nombre del bucket donde almacenaremos los respaldos.
#
# Se obtiene desde una variable de entorno para evitar
# dejar configuraciones específicas dentro del código.
#
# Si no existe la variable, el programa genera un error.

S3_BUCKET = os.environ["S3_BACKUP_BUCKET"]


# Configuración del sistema de registros.
#
# INFO: operaciones realizadas correctamente.
# ERROR: problemas durante el respaldo.
#
# Los registros permiten saber qué ocurrió
# durante la ejecución del script.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger(__name__)


# ======================================================
# 3. MÉTODO: calculate_hash()
# ======================================================

def calculate_hash(content: bytes) -> str:
    """
    Calcula la huella digital SHA-256 de un archivo.

    ¿QUÉ ES SHA-256?
    Es un algoritmo criptográfico que genera una
    representación de 64 caracteres hexadecimales
    a partir del contenido recibido.

    Nos permite comprobar si dos archivos contienen
    exactamente los mismos bytes.

    PARÁMETROS:
        content (bytes):
            Contenido completo del archivo.

    RETORNA:
        str:
            Hash SHA-256 en formato hexadecimal.

    EJEMPLO:
        hash = calculate_hash(b"Hola")
    """

    logger.info(
        "Calculando SHA-256 del contenido (%s bytes)",
        len(content)
    )

    # PASO 1:
    # Crear un objeto SHA-256 y procesar los bytes.

    hash_object = hashlib.sha256(content)

    # PASO 2:
    # Convertir el resultado a texto hexadecimal.

    file_hash = hash_object.hexdigest()

    # PASO 3:
    # Devolver el hash para utilizarlo posteriormente.

    logger.info("SHA-256 calculado correctamente")
    return file_hash


# ======================================================
# 4. MÉTODO: read_data_file()
# ======================================================

def read_data_file() -> tuple[bytes, str]:
    """
    Lee y valida el archivo pedidos.json.

    OBJETIVO:
    Evitar respaldar archivos inexistentes o
    con estructuras JSON incorrectas.

    VALIDACIONES:
    1. El archivo debe existir.
    2. Su contenido debe ser JSON válido.
    3. La raíz debe ser un objeto JSON.
    4. Debe contener una versión válida.
    5. Debe contener una lista de pedidos.

    RETORNA:
        tuple[bytes, str]:
            - Contenido original del archivo.
            - Versión de los datos.

    EXCEPCIONES:
        FileNotFoundError:
            Cuando no existe el archivo.

        ValueError:
            Cuando el contenido no cumple
            las validaciones establecidas.

        json.JSONDecodeError:
            Cuando el archivo no es JSON válido.
    """

    logger.info("Leyendo archivo de datos: %s", DATA_FILE)

    # PASO 1:
    # Verificar que el archivo existe y es un archivo.

    if not DATA_FILE.is_file():
        raise FileNotFoundError(
            f"No existe el archivo: {DATA_FILE}"
        )

    # PASO 2:
    # Leer el contenido binario original.
    #
    # Usamos bytes para que el hash corresponda
    # exactamente al contenido respaldado.

    content = DATA_FILE.read_bytes()
    logger.info("Archivo leído (%s bytes)", len(content))

    # PASO 3:
    # Interpretar el contenido como JSON.
    #
    # Si está mal formado, se genera una excepción.

    data = json.loads(content)

    # PASO 4:
    # Validar que la raíz sea un objeto JSON.

    if not isinstance(data, dict):
        raise ValueError(
            "El JSON debe contener un objeto"
        )

    # PASO 5:
    # Obtener el identificador de versión.

    version = data.get("version")

    # PASO 6:
    # Comprobar que la versión sea texto no vacío.

    if not isinstance(version, str) or not version:
        raise ValueError(
            "El campo version debe ser texto no vacío"
        )

    # PASO 7:
    # Validar los caracteres permitidos en la versión.
    #
    # Esto evita nombres inesperados en las rutas
    # de los objetos almacenados en S3.

    if not all(
        character.isascii()
        and (
            character.isalnum()
            or character in "-_"
        )
        for character in version
    ):
        raise ValueError(
            "La versión contiene caracteres inválidos"
        )

    # PASO 8:
    # Validar que pedidos sea una lista.
    #
    # El diseño contempla un conjunto de pedidos.

    if not isinstance(data.get("pedidos"), list):
        raise ValueError(
            "El campo pedidos debe ser una lista"
        )

    # PASO 9:
    # Devolver el contenido y su versión.

    logger.info(
        "JSON validado. Versión: %s; pedidos: %s",
        version,
        len(data["pedidos"])
    )
    return content, version


# ======================================================
# 5. MÉTODO: generate_backup_name()
# ======================================================

def generate_backup_name(version: str) -> str:
    """
    Genera el nombre del respaldo en S3.

    OBJETIVO:
    Identificar cada copia mediante la versión de
    los datos y el momento en que fue creada.

    FORMATO:
        backups/pedidos-VERSION-FECHA.json

    EJEMPLO:
        backups/pedidos-v1-20261121T140000000000Z.json

    PARÁMETROS:
        version (str):
            Versión del archivo de datos.

    RETORNA:
        str:
            Ruta o clave del objeto dentro de S3.
    """

    # PASO 1:
    # Obtener la fecha y hora actual en UTC.
    #
    # UTC evita depender de la zona horaria
    # del servidor que ejecuta el script.

    timestamp = datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S%fZ"
    )

    # PASO 2:
    # Construir la ruta del objeto S3.
    #
    # La carpeta backups es un prefijo lógico
    # utilizado para organizar los respaldos.

    backup_key = (
        f"backups/pedidos-{version}-{timestamp}.json"
    )

    # PASO 3:
    # Devolver la clave generada.

    logger.info("Clave de respaldo generada: %s", backup_key)
    return backup_key


# ======================================================
# 6. MÉTODO: upload_to_s3()
# ======================================================

def upload_to_s3(
    content: bytes,
    version: str,
    file_hash: str,
    backup_key: str
) -> None:
    """
    Envía el respaldo al bucket Amazon S3.

    OBJETIVO:
    Almacenar una copia independiente del archivo
    original que se encuentra en Amazon EFS.

    PARÁMETROS:
        content (bytes):
            Contenido original del archivo.

        version (str):
            Versión de los datos.

        file_hash (str):
            Hash SHA-256 del archivo.

        backup_key (str):
            Ruta del respaldo dentro del bucket.

    RETORNA:
        None:
            No devuelve datos.

    EXCEPCIONES:
        ClientError:
            Problemas relacionados con S3 o IAM.

        BotoCoreError:
            Problemas del cliente AWS.
    """

    logger.info(
        "Preparando carga a S3. Bucket: %s; clave: %s; tamaño: %s bytes",
        S3_BUCKET,
        backup_key,
        len(content)
    )

    # PASO 1:
    # Crear el cliente de Amazon S3.
    #
    # boto3 obtiene las credenciales mediante
    # la cadena de proveedores de AWS.
    #
    # Por ejemplo:
    # - Un rol IAM asociado al entorno.
    # - Un perfil AWS configurado localmente.

    s3_client = boto3.client("s3")
    logger.info("Cliente S3 creado")

    # PASO 2:
    # Subir el contenido original a S3.
    #
    # Metadata permite almacenar información
    # adicional junto al objeto.
    #
    # Guardamos:
    # - Versión de los datos.
    # - Hash SHA-256 para verificar integridad.

    s3_client.put_object(
        Bucket=S3_BUCKET,
        Key=backup_key,
        Body=content,
        ContentType="application/json",
        Metadata={
            "data-version": version,
            "sha256": file_hash
        }
    )

    # PASO 3:
    # Registrar que S3 aceptó la carga.

    logger.info(
        "Respaldo enviado a S3: s3://%s/%s",
        S3_BUCKET,
        backup_key
    )


# ======================================================
# 7. MÉTODO: create_backup()
# ======================================================

def create_backup() -> str:
    """
    Coordina la creación completa del respaldo.

    OBJETIVO:
    Ejecutar cada etapa del proceso en orden.

    FLUJO:
        Leer archivo
            ↓
        Validar información
            ↓
        Calcular SHA-256
            ↓
        Generar nombre
            ↓
        Enviar a Amazon S3
            ↓
        Registrar resultado

    RETORNA:
        str:
            Ruta del respaldo creado en S3.
    """

    logger.info("Iniciando proceso de backup")

    # PASO 1:
    # Leer y validar el archivo JSON.

    logger.info("Etapa 1/4: leer y validar archivo de datos")
    content, version = read_data_file()

    # PASO 2:
    # Calcular el hash del contenido.

    logger.info("Etapa 2/4: calcular hash de integridad")
    file_hash = calculate_hash(content)

    # PASO 3:
    # Generar un nombre para el respaldo.

    logger.info("Etapa 3/4: generar clave del respaldo")
    backup_key = generate_backup_name(version)

    # PASO 4:
    # Enviar la copia a S3.

    logger.info("Etapa 4/4: cargar respaldo a S3")
    upload_to_s3(
        content=content,
        version=version,
        file_hash=file_hash,
        backup_key=backup_key
    )

    # PASO 5:
    # Registrar la información del respaldo.

    logger.info("Backup finalizado correctamente")
    logger.info("Versión: %s", version)
    logger.info("SHA-256: %s", file_hash)
    logger.info("Ubicación: s3://%s/%s", S3_BUCKET, backup_key)

    # PASO 6:
    # Devolver la ruta del respaldo.

    return backup_key


# ======================================================
# 8. PUNTO DE ENTRADA DEL SCRIPT
# ======================================================

if __name__ == "__main__":

    # Este bloque solo se ejecuta cuando iniciamos
    # directamente el archivo backup.py.
    #
    # Ejemplo:
    # python scripts/backup.py

    try:

        # Ejecutar el proceso completo.

        create_backup()

    except (
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        BotoCoreError,
        ClientError
    ) as error:

        # Registrar el error ocurrido.

        logger.error(
            "No fue posible completar el backup: %s",
            error
        )

        # Finalizar con código de error.
        #
        # El código 1 indica que el proceso falló.

        raise SystemExit(1)
