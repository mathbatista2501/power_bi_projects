import os
import shutil
import subprocess
import re
import json
from dotenv import load_dotenv

current_folder = os.path.dirname(__file__)
debug = False

# Load environment variables from a .env file if it exists
load_dotenv()


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
            f"import -f {workspace_name}.workspace/{item_name}.{item_type} -i {staging_path}"
        )

        # Return id after deployment

        item_id = run_fab_command(
            f"get {workspace_name}.workspace/{item_name}.{item_type} -q id",
            capture_output=True,
        )

        return item_id

    print(f"::endgroup::")