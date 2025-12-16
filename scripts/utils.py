import os
import shutil
import subprocess
import re
import json
from dotenv import load_dotenv
import time
import base64
import requests
from msal import ConfidentialClientApplication

FABRIC_API_BASE = "https://api.fabric.microsoft.com/v1"

current_folder = os.path.dirname(__file__)
debug = False

# Load environment variables from a .env file if it exists
load_dotenv()

def get_fabric_token_spn() -> str:
    """Obtém token AAD para Fabric REST usando Client Credentials (SPN)."""
    tenant_id = os.environ["FABRIC_TENANT_ID"]
    client_id = os.environ["FABRIC_CLIENT_ID"]
    client_secret = os.environ["FABRIC_CLIENT_SECRET"]

    app = ConfidentialClientApplication(
        client_id=client_id,
        client_credential=client_secret,
        authority=f"https://login.microsoftonline.com/{tenant_id}"
    )
    # Usamos .default para Fabric (resolve automaticamente os scopes publicados)
    result = app.acquire_token_for_client(scopes=["https://api.fabric.microsoft.com/.default"])
    if "access_token" not in result:
        raise Exception(f"Falha no token: {result}")
    return result["access_token"]

def _poll_operation(location_url: str, token: str, timeout_sec: int = 300, interval_sec: int = 5):
    """Poll em operações LRO (quando a API retorna 202 + Location)."""
    headers = {"Authorization": f"Bearer {token}"}
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        r = requests.get(location_url, headers=headers)
        if r.status_code == 200:
            # Sucesso (a operação concluiu)
            return r.json() if r.content else None
        elif r.status_code in (202, 201):
            time.sleep(interval_sec)
            continue
        else:
            raise Exception(f"Erro ao acompanhar operação: {r.status_code} {r.text}")
    raise TimeoutError("Timeout ao aguardar operação longa (LRO).")

def _b64(file_bytes: bytes) -> str:
    return base64.b64encode(file_bytes).decode("utf-8")

def _read_file_b64(path: str) -> str:
    with open(path, "rb") as f:
        return _b64(f.read())
    
def _semantic_model_definition_from_pbip_folder(folder: str, replacements: dict | None = None) -> dict:
    """
    Monta a 'definition' do Semantic Model para REST API, baseada no conteúdo PBIP (TMDL/TMSL).
    Executa substituições (regex) nos arquivos TMDL se 'replacements' for fornecido.
    """
    parts = []

    # Tentamos o layout TMDL (mais comum no PBIP moderno)
    tmdl_files = [
        "definition/database.tmdl",
        "definition/model.tmdl"
    ]

    for rel in tmdl_files:
        full = os.path.join(folder, rel)
        if os.path.exists(full):
            content = open(full, "r", encoding="utf-8").read()
            if replacements:
                for (file_rel, pattern), replacement in replacements.items():
                    if rel.endswith(file_rel):
                        content = re.sub(pattern, replacement, content, flags=re.MULTILINE)
            parts.append({"path": rel, "payload": _b64(content.encode("utf-8")), "payloadType": "InlineBase64"})

    # Outros possíveis TMDL (tabelas)
    tmdl_dir = os.path.join(folder, "definition", "tables")
    if os.path.isdir(tmdl_dir):
        for root, _, files in os.walk(tmdl_dir):
            for f in files:
                rel_path = os.path.relpath(os.path.join(root, f), folder)
                content = open(os.path.join(root, f), "r", encoding="utf-8").read()
                if replacements:
                    for (file_rel, pattern), replacement in replacements.items():
                        if rel_path.endswith(file_rel):
                            content = re.sub(pattern, replacement, content, flags=re.MULTILINE)
                parts.append({"path": rel_path, "payload": _b64(content.encode("utf-8")), "payloadType": "InlineBase64"})

    # definition.pbism (metadados)
    pbism = os.path.join(folder, "definition.pbism")
    if os.path.exists(pbism):
        parts.append({"path": "definition.pbism", "payload": _read_file_b64(pbism), "payloadType": "InlineBase64"})

    # diagramLayout.json (opcional)
    diagram = os.path.join(folder, "diagramLayout.json")
    if os.path.exists(diagram):
        parts.append({"path": "diagramLayout.json", "payload": _read_file_b64(diagram), "payloadType": "InlineBase64"})

    # .platform (opcional; atualiza metadados se usado com updateMetadata=true)
    platform = os.path.join(folder, ".platform")
    if os.path.exists(platform):
        parts.append({"path": ".platform", "payload": _read_file_b64(platform), "payloadType": "InlineBase64"})

    if not parts:
        raise Exception("Nenhum arquivo TMDL/TMSL encontrado para montar a definição do Semantic Model.")

    return {"parts": parts}  # conforme doc de definição de Semantic Model [5](https://learn.microsoft.com/en-us/rest/api/fabric/articles/item-management/definitions/semantic-model-definition)    

def _report_definition_from_pbip_folder(folder: str, definition_pbir_json: dict) -> dict:
    """
    Monta a 'definition' do Report para REST API, garantindo 'definition.pbir' em modo byConnection (exigido pela API).
    """
    parts = []

    # definition.pbir (byConnection)
    pbir_payload = json.dumps(definition_pbir_json, ensure_ascii=False).encode("utf-8")
    parts.append({"path": "definition.pbir", "payload": _b64(pbir_payload), "payloadType": "InlineBase64"})

    # report.json (PBIR-Legacy) ou a pasta 'definition/...'(PBIR). Aqui tentamos report.json.
    report_json = os.path.join(folder, "report.json")
    if os.path.exists(report_json):
        parts.append({"path": "report.json", "payload": _read_file_b64(report_json), "payloadType": "InlineBase64"})

    # .platform (opcional)
    platform = os.path.join(folder, ".platform")
    if os.path.exists(platform):
        parts.append({"path": ".platform", "payload": _read_file_b64(platform), "payloadType": "InlineBase64"})

    # Observação: a API REST para Reports **só suporta byConnection** em definition.pbir (não byPath) [6](https://learn.microsoft.com/en-us/rest/api/fabric/articles/item-management/definitions/report-definition)
    return {"parts": parts}

def create_or_update_semantic_model(workspace_id: str, display_name: str, definition: dict, token: str) -> str:
    """
    Cria o Semantic Model (se não existe) ou atualiza sua definição (se já existe).
    Retorna o semanticModelId.
    """
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # 1) Tentamos criar
    url_create = f"{FABRIC_API_BASE}/workspaces/{workspace_id}/semanticModels"
    body = {"displayName": display_name, "definition": definition}
    r = requests.post(url_create, headers=headers, json=body)
    if r.status_code in (200, 201):
        return r.json()["id"]
    if r.status_code == 202:
        # Operação longa: poll
        loc = r.headers.get("Location")
        _poll_operation(loc, token)
        # Depois da criação, listamos para obter o id
        # (Poderíamos usar o Location final, mas garantimos pelo GET)
    elif r.status_code == 409:
        # Nome em uso: precisamos localizar o item e fazer updateDefinition
        pass
    elif r.status_code >= 400:
        raise Exception(f"Falha ao criar SemanticModel: {r.status_code} {r.text}")

    # 2) Descobrir o ID pelo nome (List semanticModels)
    url_list = f"{FABRIC_API_BASE}/workspaces/{workspace_id}/semanticModels"
    rl = requests.get(url_list, headers=headers)
    if rl.status_code != 200:
        raise Exception(f"Falha ao listar SemanticModels: {rl.status_code} {rl.text}")
    items = rl.json().get("value", [])
    existing = next((i for i in items if i.get("displayName") == display_name), None)
    if not existing:
        raise Exception(f"SemanticModel '{display_name}' não localizado após criação/409.")
    sm_id = existing["id"]

    # 3) Atualizar definição (override)
    url_update = f"{FABRIC_API_BASE}/workspaces/{workspace_id}/semanticModels/{sm_id}/updateDefinition"
    ru = requests.post(url_update, headers=headers, json={"definition": definition})
    if ru.status_code == 202:
        _poll_operation(ru.headers.get("Location"), token)  # LRO
    elif ru.status_code not in (200, 201):
        raise Exception(f"Falha ao atualizar definição do SemanticModel: {ru.status_code} {ru.text}")

    return sm_id  # conforme doc: updateDefinition para SM [2](https://learn.microsoft.com/en-us/rest/api/fabric/semanticmodel/items/update-semantic-model-definition)

def create_or_update_report(workspace_id: str, display_name: str, definition: dict, token: str) -> str:
    """
    Cria o Report (se não existe) ou atualiza sua definição.
    Retorna o reportId.
    """
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # 1) Tentamos criar
    url_create = f"{FABRIC_API_BASE}/workspaces/{workspace_id}/reports"
    body = {"displayName": display_name, "definition": definition}
    r = requests.post(url_create, headers=headers, json=body)
    if r.status_code in (200, 201):
        return r.json()["id"]
    if r.status_code == 202:
        _poll_operation(r.headers.get("Location"), token)
    elif r.status_code == 409:
        pass
    elif r.status_code >= 400:
        raise Exception(f"Falha ao criar Report: {r.status_code} {r.text}")

    # 2) Descobrir ID pelo nome (List reports)
    url_list = f"{FABRIC_API_BASE}/workspaces/{workspace_id}/reports"
    rl = requests.get(url_list, headers=headers)
    if rl.status_code != 200:
        raise Exception(f"Falha ao listar Reports: {rl.status_code} {rl.text}")
    items = rl.json().get("value", [])
    existing = next((i for i in items if i.get("displayName") == display_name), None)
    if not existing:
        raise Exception(f"Report '{display_name}' não localizado após criação/409.")
    rep_id = existing["id"]

    # 3) Atualizar definição do report
    url_update = f"{FABRIC_API_BASE}/workspaces/{workspace_id}/reports/{rep_id}/updateDefinition"
    ru = requests.post(url_update, headers=headers, json={"definition": definition})
    if ru.status_code == 202:
        _poll_operation(ru.headers.get("Location"), token)
    elif ru.status_code not in (200, 201):
        raise Exception(f"Falha ao atualizar definição do Report: {ru.status_code} {ru.text}")

    return rep_id  # conforme doc: updateDefinition




def fab_authenticate_spn():
    """
    Autentica com Service Principal usando variáveis de ambiente:
    FABRIC_CLIENT_ID, FABRIC_CLIENT_SECRET, FABRIC_TENANT_ID.

    Ordem correta em CI:
      1) habilitar fallback de criptografia do cache de token
      2) fazer login do SPN
    """

    client_id = os.getenv("FABRIC_CLIENT_ID")
    client_secret = os.getenv("FABRIC_CLIENT_SECRET")
    tenant_id = os.getenv("FABRIC_TENANT_ID")

    print("Authenticating with SPN")

    if not all([client_id, client_secret, tenant_id]):
        raise Exception("FABRIC_CLIENT_ID, FABRIC_CLIENT_SECRET and FABRIC_TENANT_ID are required")

    # 1) Habilitar fallback ANTES do login — chave correta (sem 'fab_')
    # Docs do Fabric CLI: 'encryption_fallback_enabled'
    # Exemplos oficiais: 'fab config set encryption_fallback_enabled true'
    run_fab_command("config set encryption_fallback_enabled true")

    # (opcional) confirmar valor
    run_fab_command("config get encryption_fallback_enabled", capture_output=True)

    # 2) Login SPN
    run_fab_command(
        f"auth login -u {client_id} -p {client_secret} --tenant {tenant_id}",
        include_secrets=True
    )

    print("SPN authenticated successfully!")



def run_fab_command(
    command: str,
    capture_output: bool = False,
    include_secrets: bool = False,
    silently_continue: bool = False
):
    """
    Executa um comando do Fabric CLI (fab) em modo não interativo.

    Parâmetros:
        command (str): subcomando do 'fab' (ex.: "config set encryption_fallback_enabled true").
        capture_output (bool): se True, retorna stdout do processo.
        include_secrets (bool): se True, imprime valores sensíveis em logs de debug (evitar em CI).
        silently_continue (bool): se True, não lança exceção em caso de erro; apenas retorna stdout/stderr.

    Retorno:
        str | None: stdout completo (strip) se capture_output=True; caso contrário, None.

    Exceções:
        Exception: se o comando falhar (exit_code != 0) e silently_continue=False.
    """

    # Converte o comando em lista de args segura (evita parsing frágil com shell=True)
    args = ["fab", *command.split()]

    result = subprocess.run(
        args,
        capture_output=True,  # sempre capturar para poder depurar com mensagens úteis
        text=True
    )

    exit_code = result.returncode
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()

    # Monta uma linha de comando "safe" para log (removendo segredos se necessário)
    cmd_for_log = " ".join(args)
    if not include_secrets:
        # Tentativa de mascarar segredos comuns (-p <secret>, --password, etc.)
        cmd_for_log = cmd_for_log.replace("-p", "-p *****")

    # Se deu erro e não é para continuar silenciosamente, lança exceção
    if not silently_continue and (exit_code != 0):
        raise Exception(
            "Error running fab command.\n"
            f"  command: {cmd_for_log}\n"
            f"  exit_code: {exit_code}\n"
            f"  stdout:\n{stdout}\n"
            f"  stderr:\n{stderr}\n"
        )

    # Se quiser o output, retorna stdout completo
    if capture_output:
        return stdout

    return None



def create_workspace(workspace_name, capacity_name: str = "none", upns: list = None):
    """
    Creates a new workspace with the specified name and optional capacity.
    Additionally, assigns admin roles to the provided user principal names (UPNs).
    Args:
        workspace_name (str): The name of the workspace to be created.
        capacity_name (str, optional): The name of the capacity to assign to the workspace. Defaults to None.
        upns (list, optional): A list of user principal names to be assigned as admins to the workspace. Defaults to None.
    Returns:
        None
    """

    print(f"::group::Creating workspace: {workspace_name}")

    command = f"create /{workspace_name}.Workspace"

    if capacity_name:
        command += f" -P capacityName={capacity_name}"

    run_fab_command(command, silently_continue=True)

    if upns is not None:

        upns = [x for x in upns if x.strip()]

        if len(upns) > 0:
            print(f"Adding UPNs")

            for upn in upns:
                run_fab_command(f"acl set -f /{workspace_name}.Workspace -I {upn} -R admin")

    print(f"::endgroup::")


def copy_to_staging(path):
    """
    Copies the contents of the specified directory to a staging folder.
    This function ensures that a staging folder exists, and if it already exists,
    it removes the existing staging folder and creates a new one. It then copies
    all files and directories from the specified path to the staging folder.
    Args:
        path (str): The path of the directory to be copied to the staging folder.
    Returns:
        str: The path to the staging folder where the contents have been copied.
    """

    # ensure staging folder exists

    path_staging = os.path.join(current_folder, "_stg", os.path.basename(path))

    if os.path.exists(path_staging):
        shutil.rmtree(path_staging)

    os.makedirs(path_staging)

    # copy files to staging folder

    shutil.copytree(path, path_staging, dirs_exist_ok=True)

    return path_staging


def read_pbip_jsonfile(path):
    """
    Reads a JSON file from the specified path and returns its contents as a dictionary.
    Args:
        path (str): The file path to the JSON file.
    Returns:
        dict: The contents of the JSON file.
    Raises:
        Exception: If the file does not exist at the specified path.
    """

    if not os.path.exists(path):
        raise Exception(f"Cannot find file: '{path}'")

    with open(path, "r", encoding="utf-8") as file:
        data = json.load(file)

    return data


def deploy_item(
    src_path,
    workspace_name,
    item_type: str = None,
    item_name: str = None,
    find_and_replace: dict = None,
    what_if: bool = False,
    func_after_staging=None,
):
    """
    Deploys an item to a specified workspace.
    Args:
        src_path (str): The source path of the item to be deployed.
        workspace_name (str): The name of the workspace where the item will be deployed.
        item_type (str, optional): The type of the item. If not provided, it will be inferred from the platform data.
        item_name (str, optional): The name of the item. If not provided, it will be inferred from the platform data.
        find_and_replace (dict, optional): A dictionary where keys are tuples containing a file filter regex and a find regex,
                                           and values are the replacement strings. This will be used to perform find and replace
                                           operations on the files in the staging path.
        what_if (bool, optional): If True, the deployment will be simulated but not actually performed. Defaults to False.
        func_after_staging (callable, optional): A function to be called after the item is copied to the staging path. It should
                                                 accept the staging path as its only argument.
    Returns:
        str: The ID of the deployed item if `what_if` is False. Otherwise, returns None.
    """

    staging_path = copy_to_staging(src_path)

    # Call function that provides flexibility to change something in the staging files

    if func_after_staging:
        func_after_staging(staging_path)

    if os.path.exists(os.path.join(staging_path, ".platform")):

        with open(os.path.join(staging_path, ".platform"), "r", encoding="utf-8") as file:
            platform_data = json.load(file)

        if item_name is None:
            item_name = platform_data["metadata"]["displayName"]

        if item_type is None:
            item_type = platform_data["metadata"]["type"]

    # Loop through all files and apply the find & replace with regular expressions

    if find_and_replace:

        for root, _, files in os.walk(staging_path):
            for file in files:

                file_path = os.path.join(root, file)

                with open(file_path, "r", encoding="utf-8", errors='replace') as file:
                    text = file.read()

                # Loop parameters and execute the find & replace in the ones that match the file path

                for key, replace_value in find_and_replace.items():

                    find_and_replace_file_filter = key[0]

                    find_and_replace_file_find = key[1]

                    if re.search(find_and_replace_file_filter, file_path):
                        text, count_subs = re.subn(
                            find_and_replace_file_find, replace_value, text
                        )

                        if count_subs > 0:

                            print(
                                f"Find & replace in file '{file_path}' with regex '{find_and_replace_file_find}'"
                            )

                            with open(file_path, "w", encoding="utf-8") as file:
                                file.write(text)
    if not what_if:
        run_fab_command(
            f"import -f {workspace_name}.Workspace/{item_name}.{item_type} -i {staging_path}"
        )

        # Return id after deployment

        item_id = run_fab_command(
            f"get {workspace_name}.Workspace/{item_name}.{item_type} -q id",
            capture_output=True,
        )

        return item_id

    print(f"::endgroup::")