

import os
import sys
import json
import logging
import shutil
import tempfile
import time
from pathlib import Path

import boto3
from moto import mock_aws

# Mostrar todos los mensajes del flujo del test en la consola.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("test_dba")

# 1. Configurar AWS simulado
logger.info("Configurando credenciales y bucket de AWS simulado")
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
os.environ["S3_BACKUP_BUCKET"] = "nodo-ruta-test"

# 2. Importar nuestros scripts DBA
sys.path.insert(0, str(Path("scripts").resolve()))

logger.info("Cargando módulos backup, restore y verify")
import backup
import restore
import verify

# 3. Crear una copia temporal de pedidos.json
logger.info("Creando copia temporal del archivo de datos")
with tempfile.TemporaryDirectory() as directory:
    data_file = Path(directory) / "pedidos.json"
    shutil.copyfile("data/pedidos.json", data_file)

    original = data_file.read_bytes()

    backup.DATA_FILE = data_file
    restore.DATA_FILE = data_file
    verify.DATA_FILE = data_file
    logger.info("Archivo temporal configurado: %s", data_file)

    # 4. Simular Amazon S3
    logger.info("Iniciando simulación de Amazon S3 con moto")
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="nodo-ruta-test")

        s3.put_bucket_versioning(
            Bucket="nodo-ruta-test",
            VersioningConfiguration={"Status": "Enabled"}
        )
        logger.info("Bucket de prueba creado y versionado")

        # 5. Crear respaldo
        logger.info("[1/5] Creando respaldo...")
        key = backup.create_backup()
        logger.info("Respaldo de prueba creado: %s", key)

        # 6. Alterar el archivo temporal
        logger.info("[2/5] Simulando pérdida de integridad...")
        data = json.loads(data_file.read_bytes())
        data["pedidos"][0]["estado"] = "CORRUPTO"
        data_file.write_text(
            json.dumps(data),
            encoding="utf-8"
        )
        logger.info("Archivo temporal alterado intencionalmente")

        # 7. Comprobar que detectamos el cambio
        logger.info("[3/5] Detectando alteración...")
        assert verify.execute_verification(key) is False
        logger.info("Alteración detectada correctamente")

        # 8. Restaurar desde S3
        logger.info("[4/5] Restaurando datos...")
        start = time.monotonic()
        restore.execute_restoration(key)
        duration = time.monotonic() - start
        logger.info("Restauración finalizada en %.3f segundos", duration)

        # 9. Verificar integridad final
        logger.info("[5/5] Verificando restauración...")
        assert verify.execute_verification(key) is True
        assert data_file.read_bytes() == original
        logger.info("Contenido restaurado coincide byte a byte con el original")

        recovered = json.loads(data_file.read_bytes())
        assert recovered["version"] == "v1"
        assert len(recovered["pedidos"]) == 6

        logger.info("TODAS LAS PRUEBAS PASARON")
        logger.info("Versión restaurada: %s", recovered["version"])
        logger.info("Pedidos recuperados: %s", len(recovered["pedidos"]))
        logger.info("Tiempo de recuperación: %.3f segundos", duration)
