import os
import argparse
import glob
from utils import *

parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument("--spn-auth", action="store_true", default=True)
parser.add_argument("--environment", default="main")
parser.add_argument("--config-file", default="./config.json")
parser.add_argument("--capacity", default=None, help="Capacity name")
parser.add_argument("--workspace", default=None, help="Workspace name")
parser.add_argument("--admin-upns", default=None, help="Comma-separated list of admin UPNs")

args = parser.parse_args()

current_file = __file__
current_folder = os.path.dirname(current_file)
src_folder = os.path.join(current_folder, "..", "src")

# Deployment parameters:
spn_auth = args.spn_auth
environment = args.environment
capacity_name = args.capacity
workspace_name = args.workspace
admin_upns = args.admin_upns.split(",") if args.admin_upns else []

config = read_pbip_jsonfile(args.config_file)
configEnv = config[args.environment]

# Use command-line arguments if provided, otherwise fallback to config values
capacity_name = capacity_name or configEnv.get("capacity")
workspace_name = workspace_name or configEnv["workspace"]
admin_upns = admin_upns or configEnv.get("adminUPNs", "").split(",")

semanticmodel_parameters = configEnv.get("semanticModelsParameters", None)
server = semanticmodel_parameters.get("SqlServerInstance", None)
database = semanticmodel_parameters.get("SqlServerDatabase", None)

# Authentication
if spn_auth:
    fab_authenticate_spn()



# Autenticação (REST)
token = get_fabric_token_spn()  # token para REST APIs (audience api.fabric) [7](https://learn.microsoft.com/en-us/rest/api/fabric/articles/)

# ⚠️ Você precisa do workspaceId (GUID) para as APIs
# Sugestões:
# - adicionar --workspace-id nos args (já incluído nos patches anteriores)
# - ou resolver via config.json
workspace_id = configEnv.get("workspaceId")
if not workspace_id:
    print("Erro: workspaceId ausente no config.json (necessário para REST).", file=sys.stderr)
    sys.exit(2)

# ----- Semantic Model via API -----
src_folder = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "src"))
semantic_model_folder = os.path.join(src_folder, "FP_Analysis.SemanticModel")

# Monta replacements com os regex já usados
replacements = {
    (r"expressions.tmdl", r'(expression\s+SqlServerInstance\s*=\s*)".*?"'): rf'\1"{server}"' if server else None,
    (r"expressions.tmdl", r'(expression\s+SqlServerDatabase\s*=\s*)".*?"'): rf'\1"{database}"' if database else None,
}
# remove entries None
replacements = {k: v for k, v in replacements.items() if v is not None}

sm_definition = _semantic_model_definition_from_pbip_folder(
    semantic_model_folder,
    replacements=replacements or None
)

semanticmodel_display_name = "FP_Analysis.SemanticModel"  # destino (nome exibido)
semanticmodel_id = create_or_update_semantic_model(
    workspace_id=workspace_id,
    display_name=semanticmodel_display_name,
    definition=sm_definition,
    token=token
)

# ----- Reports via API -----

# ----- Deploy dos Reports via REST -----
for report_path in glob.glob(os.path.join(src_folder, "*.Report")):
    report_name = os.path.basename(report_path.rstrip("/"))  # ex.: MyReport.Report

    # Monta o JSON do definition.pbir (byConnection) exigido pela REST API
    definition_pbir = {
        "version": "4.0",
        "datasetReference": {
            "byConnection": {
                "connectionString": None,
                "pbiServiceModelId": None,
                "pbiModelVirtualServerName": "sobe_wowvirtualserver",
                "pbiModelDatabaseName": semanticmodel_id,  # usa o ID retornado do SM
                "name": "EntityDataSource",
                "connectionType": "pbiServiceXmlaStyleLive",
            }
        },
    }

    # Constrói a definition do report (PBIR-Legacy com report.json + definition.pbir)
    rep_definition = _report_definition_from_pbip_folder(
        folder=report_path,
        definition_pbir_json=definition_pbir
    )

    # Cria/atualiza via REST
    create_or_update_report(
        workspace_id=workspace_id,
        display_name=report_name,
        definition=rep_definition,
        token=token
    )







run_fab_command("auth logout")