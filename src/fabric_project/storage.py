from pathlib import Path
import pickle, pandas as pd
from ipfs_interface import IpfsInterface

ipfsDB = IpfsInterface()   
_results_path = None       # set on init

def init_storage(test_dir):
    global _results_path
    _results_path = test_dir / "results.xlsx"
    _results_path.parent.mkdir(exist_ok=True)

def save_results(epoch, epoch_time, accuracy, filepath):
    excel_file = Path(filepath)
    results = pd.DataFrame({'Epoch': [epoch], 'Time (seconds)': [epoch_time], 'Accuracy (%)': [accuracy]})
    if excel_file.exists():
        existing_results = pd.read_excel(excel_file)
        results = pd.concat([existing_results, results], ignore_index=True)
    results.to_excel(excel_file, index=False)

def store_data_and_generate_hash(data):
    raw = pickle.dumps(data)
    return ipfsDB.upload_bytes(raw, name = "payload.pkl")

def retrieve_data_by_hash(cid):
    raw = ipfsDB.cat_bytes(cid)
    if not raw:
        raise KeyError(f"IPFS: no data for {cid}")
    return pickle.loads(raw)

# ipfsDB = {}

# def store_data_and_generate_hash(data):
#     serialized_data = pickle.dumps(data)
#     hash_object = hashlib.sha256(serialized_data)
#     data_hash = hash_object.hexdigest()
#     ifpsDB[data_hash] = data
#     return data_hash

# def retrieve_data_by_hash(data_hash):
#     data = ifpsDB.get(data_hash, None)
#     if data is not None:
#         return data
#     else:
#         raise KeyError("Data not found for hash:", data_hash)

