import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Dict, Optional, Set, Union

import requests
import yaml


class ConfigAction(argparse.Action):
    def __init__(
        self,
        option_strings="",
        dest="",
        nargs=None,
        const=None,
        default=None,
        type=None,
        choices=None,
        required=False,
        help=None,
        metavar=None,
    ):
        super(ConfigAction, self).__init__(
            option_strings=option_strings,
            dest=dest,
            nargs=nargs,
            const=const,
            default=default,
            type=type,
            choices=choices,
            required=required,
            help=help,
            metavar=metavar,
        )

    def __call__(self, parser, namespace, values: Path, option_string=None) -> None:
        path = values
        if values.is_dir():
            path = values / "config.yml"
        if not path.exists():
            path = Path(__file__).parent / "config.yml"
            if not path.exists():
                return

        with open(path, "r") as file:
            config = yaml.load(file.read(), Loader=yaml.CLoader)

        keys = ["api_key", "host", "json"]
        for key in keys:
            try:
                setattr(namespace, key, config[key])
            except KeyError:
                item = getattr(namespace, key, None)
                setattr(namespace, key, item)


parser = argparse.ArgumentParser(conflict_handler="resolve")
mutual_exlusion_group = parser.add_mutually_exclusive_group()
parser.add_argument(
    "--config",
    action=ConfigAction,
    type=Path,
    help="Path to a YAML config file. "
    "Will look for a config.yml beside %(prog)s by default",
    metavar="<path to .yml>",
)
parser.add_argument(
    "--json",
    type=Path,
    help="Path to a JSON file for storing and loading album IDs. "
    "When specified it will update existing albums instead of creating new ones each script execution",
    metavar="<path to .json>",
)
parser.add_argument(
    "--api-key",
    help="The API key generated under account settings in the immich app. "
    "Required unless specified in the config",
)
parser.add_argument(
    "--host",
    help="URL to the Immich instance. Example: http://localhost:2283"
)
parser.add_argument(
    "-l", "--library", nargs="+", help="List of library names to only run the script on", metavar="LIBRARY NAMES"
)
parser.add_argument(
    "-f",
    "--folder-layout",
    action="store_true",
    help="Use the name of the folder an asset is in for the album name instead of the library name",
)
parser.add_argument(
    "-s",
    "--skip-paths",
    nargs="+",
    type=Path,
    default=[],
    help="List of paths to ignore. " 'Add "*" at the end to also ignore subfolders',
    metavar="<paths to skip>"
)
mutual_exlusion_group.add_argument(
    "-c",
    "--clean-update",
    action="store_true",
    help="Also clear the album of wrongly indexed assets",
)
mutual_exlusion_group.add_argument(
    "--skip-existing", action="store_true", help="Ignore albums that already exist"
)
args = parser.parse_args()
if args.config is None and args.api_key is None and args.host is None:
    args = parser.parse_args(
        ["--config", str(Path(__file__).parent / "config.yml")], namespace=args
    )
if args.api_key is None or args.host is None:
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--host", required=True)
    args = parser.parse_args(
        (
            ["--config", str(Path(__file__).parent / "config.yml")]
            if args.config is None
            else None
        ),
        namespace=args,
    )

host: str = args.host.rstrip("/")
if not host.endswith("api"):
    host += "/api"

json_folder_layout: Optional[dict] = None
json_name_layout: Optional[dict] = None
if args.json is not None:
    if args.json.exists():
        with open(args.json, "r") as json_file:
            old_json_data: dict = json.load(json_file)
            json_folder_layout = old_json_data.get("folder_layout", None)
            json_name_layout = old_json_data.get("name_layout", None)
    elif not args.json.parent.exists():
        sys.exit("The given json path is invalid.")

headers = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "x-api-key": args.api_key,
}
print("Making API Requests")
print()
libraries = requests.get(
    host + "/library", headers=headers, params={"type": "EXTERNAL"}
).json()
print(f"Found {len(libraries)} Libraries")
if args.library is not None:
    library_filter = []
    library_names = [x["name"] for x in libraries]

    for item in args.library:
        if item not in library_names:
            print(f'Unable to find a library by the name of "{item}"')
            sys.exit()

        library_filter.append(
            next((l["id"] for l in libraries if l["name"] == item), "")
        )
else:
    library_filter = [x["id"] for x in libraries]

albums: dict = requests.get(host + "/album", headers=headers).json()
print(f"Found {len(albums)} Albums")
assets: dict = requests.get(host + "/asset", headers=headers).json()
print(f"Found {len(assets)} Assets")
print()

album_names = [a["albumName"] for a in albums]
album_ids = [a["id"] for a in albums]
skip_paths = {"direct": [], "recursive": []}
for p in args.skip_paths:
    if p.stem == "*":
        skip_paths["recursive"].append(p.parent)
    else:
        skip_paths["direct"].append(p)


def create_album() -> None:
    if args.skip_existing and args.json is None:
        if album_name in album_names:
            print(f'Album "{album_name}" already exists. Skipping')
            return

    payload = json.dumps({"albumName": album_name, "assetIds": list(asset_ids)})
    r = requests.post(host + "/album", headers=headers, data=payload)
    if r.ok:
        print(f'Created album "{album_name}" with {len(asset_ids)} assets')
        if args.json:
            json_output.update({update_key: r.json()["id"]})
    else:
        print(
            f'[ERROR] Creation of album "{album_name}" failed with error code: {r.status_code} {r.reason}'
        )
        print(r.json()["message"][0])


def update_album() -> None:
    if args.skip_existing and album_id in album_ids:
        print(f'Album "{album_name}" already exists. Skipping')
        return

    payload = json.dumps({"ids": list(asset_ids)})
    r = requests.put(
        host + "/album" + f"/{album_id}" + "/assets", headers=headers, data=payload
    )
    if r.ok:
        count = 0
        for a in r.json():
            if a["success"]:
                count += 1
        if count == 0:
            print(f'Album "{album_name}" is already up to date.')
        else:
            print(
                f'Added {count} asset{"s" if len(str(count)) > 1 else ""} to the album "{album_name}"'
            )
    else:
        print(
            f'[ERROR] Updating album "{album_name}" failed with error code: {r.status_code} {r.reason}'
        )
        msg = r.json()["message"]
        if isinstance(msg, list):
            msg = msg[0]
        print(msg)


def clean_album() -> None:
    r = requests.get(host + "/album" + f"/{album_id}", headers=headers)
    if r.ok:
        album_assets = r.json()
        removal_assets = set([a["id"] for a in album_assets["assets"]])
        removal_assets.difference_update(asset_ids)
    else:
        print(
            f'[ERROR] Clearing album "{album_name} failed with error code: {r.status_code} {r.reason}"'
        )
        print(r.json()["message"])
        return

    payload = json.dumps({"ids": list(removal_assets)})
    r = requests.delete(
        host + "/album" + f"/{album_id}" + "/assets", headers=headers, data=payload
    )
    if r.ok:
        count = 0
        for a in r.json():
            if a["success"]:
                count += 1
        if count != 0:
            print(f'Cleared {count} assets from "{album_name}"')


json_output = {}

if args.folder_layout:
    folder_assets: Dict[Path, Set[str]] = {}

    if json_folder_layout is not None:
        json_output = deepcopy(json_folder_layout)

    for asset in assets:
        if asset["libraryId"] not in library_filter:
            continue

        path = Path(asset["originalPath"]).parent
        if path in skip_paths["direct"]:
            continue
        if [p for p in path.parents if p in skip_paths["recursive"]]:
            continue

        try:
            folder_assets[path]
        except:
            folder_assets[path] = set()
        finally:
            folder_assets[path].add(asset["id"])

    for path, path_items in folder_assets.items():
        album_name = path.stem
        asset_ids = path_items
        update_key = str(path)
        json_output[update_key] = {}

        if json_folder_layout is not None:
            try:
                album_id = json_folder_layout[update_key]
            except:
                pass
            else:
                if album_id in album_ids:
                    if args.clean_update:
                        clean_album()
                    json_output.update({update_key: album_id})
                    update_album()
                    continue

        create_album()
else:
    name_assets: Dict[str, Dict[str, Set[Union[Path, str]]]] = {}

    if json_name_layout is not None:
        json_output = deepcopy(json_name_layout)

    for asset in assets:
        if asset["libraryId"] not in library_filter:
            continue

        path = Path(asset["originalPath"]).parent
        if path in skip_paths["direct"]:
            continue
        if [p for p in path.parents if p in skip_paths["recursive"]]:
            continue

        try:
            name_assets[asset["libraryId"]]
        except:
            name_assets[asset["libraryId"]] = {"paths": set(), "ids": set()}
        finally:
            name_assets[asset["libraryId"]]["paths"].add(path)
            name_assets[asset["libraryId"]]["ids"].add(asset["id"])

    for lib_id, lib_items in name_assets.items():
        album_name = next((l["name"] for l in libraries if l["id"] == lib_id), "")
        asset_ids = lib_items["ids"]
        update_key = lib_id
        json_output[update_key] = {}

        if json_name_layout is not None:
            try:
                album_id = json_name_layout[update_key]
            except:
                pass
            else:
                if album_id in album_ids:
                    if args.clean_update:
                        clean_album()
                    json_output.update({update_key: album_id})
                    update_album()
                    continue

        create_album()

if args.json is not None:
    try:
        json_data = old_json_data
    except:
        json_data = {}

    if args.folder_layout:
        json_data.update({"folder_layout": json_output})
    else:
        json_data.update({"name_layout": json_output})

    with open(args.json, "w+") as json_file:
        json.dump(json_data, json_file, indent=4)

print()
print("Done!")
