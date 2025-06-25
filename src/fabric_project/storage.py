from pathlib import Path
import pickle

import pandas as pd

from ipfs_interface import IpfsInterface

_ipfs = IpfsInterface()


def init_storage(test_dir: Path) -> None:
    results_path = test_dir / "results.xlsx"
    results_path.parent.mkdir(parents=True, exist_ok=True)


def save_results(
    epoch: int,
    epoch_time: float,
    accuracy: float,
    filepath: str,
) -> None:
    excel_file = Path(filepath)
    new_row = pd.DataFrame(
        {
            "Epoch": [epoch],
            "Time (seconds)": [epoch_time],
            "Accuracy (%)": [accuracy],
        }
    )
    if excel_file.exists():
        existing = pd.read_excel(excel_file)
        new_row = pd.concat([existing, new_row], ignore_index=True)
    new_row.to_excel(excel_file, index=False)


def store_data_and_generate_hash(data) -> str:
    payload = pickle.dumps(data)
    return _ipfs.upload_bytes(payload, name="payload.pkl")


def retrieve_data_by_hash(cid: str):
    raw = _ipfs.cat_bytes(cid)
    if not raw:
        raise KeyError(f"IPFS: no data for {cid}")
    return pickle.loads(raw)
