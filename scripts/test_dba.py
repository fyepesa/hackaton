

import os
import sys
import json
import shutil
import tempfile
import time
from pathlib import Path

import boto3
from moto import mock_aws

# 1. Configurar AWS simulado
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
os.environ["S3_BACKUP_BUCKET"] = "nodo-ruta-test"

# 2. Importar nuestros scripts DBA
sys.path.insert(0, str(Path("scripts").resolve()))

import backup
import restore
import verify

# 3. Crear una copia temporal de pedidos.json
with tempfile.TemporaryDirectory() as directory:
    data_file = Path(directory) / "pedidos.json"
    shutil.copyfile("data/pedidos.json", data_file)

    original = data_file.read_bytes()

    backup.DATA_FILE = data_file
    restore.DATA_FILE = data_file
    verify.DATA_FILE = data_file

    # 4. Simular Amazon S3
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="nodo-ruta-test")

        s3.put_bucket_versioning(
            Bucket="nodo-ruta-test",
            VersioningConfiguration={"Status": "Enabled"}
        )

        # 5. Crear respaldo
        print("\n[1] Creando respaldo...")
        key = backup.create_backup()

        # 6. Alterar el archivo temporal
        print("\n[2] Simulando pérdida de integridad...")
        data = json.loads(data_file.read_bytes())
        data["pedidos"][0]["estado"] = "CORRUPTO"
        data_file.write_text(
            json.dumps(data),
            encoding="utf-8"
        )

        # 7. Comprobar que detectamos el cambio
        print("\n[3] Detectando alteración...")
        assert verify.execute_verification(key) is False
        print("Alteración detectada correctamente")

        # 8. Restaurar desde S3
        print("\n[4] Restaurando datos...")
        start = time.monotonic()
        restore.execute_restoration(key)
        duration = time.monotonic() - start

        # 9. Verificar integridad final
        print("\n[5] Verificando restauración...")
        assert verify.execute_verification(key) is True
        assert data_file.read_bytes() == original

        recovered = json.loads(data_file.read_bytes())
        assert recovered["version"] == "v1"
        assert len(recovered["pedidos"]) == 6

        print("\n✅ TODAS LAS PRUEBAS PASARON")
        print("Versión restaurada:", recovered["version"])
        print("Pedidos recuperados:", len(recovered["pedidos"]))
        print(f"Tiempo de recuperación: {duration:.3f} segundos")
PY
