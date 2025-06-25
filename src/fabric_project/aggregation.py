from collections import OrderedDict
import logging
import threading
import time
import torch

from storage import store_data_and_generate_hash, retrieve_data_by_hash
from config import NUM_CLIENTS

# Synchronization primitives and data holders
local_update_barrier = threading.Barrier(NUM_CLIENTS + 1)
client_aggregation_events = {i: threading.Event() for i in range(NUM_CLIENTS)}
client_updates_for_aggregation = {}
# Stores global model hash per round
global_model_hashes_by_round = {}

def perform_aggregation(round_id: int, num_expected_clients: int) -> None:
    """Average valid client model updates and signal clients."""
    print(f"\n--- Aggregator: Starting Aggregation for Round {round_id} ---")
    updates = client_updates_for_aggregation.get(round_id, [])

    if not updates:
        print("Aggregator: No client updates received for this round.")
        previous_hash = global_model_hashes_by_round.get(round_id - 1)
        if previous_hash:
            global_model_hashes_by_round[round_id] = previous_hash
            print(
                f"Aggregator: Carrying over model hash from round {round_id-1}."
            )
        else:
            print("Aggregator: WARNING - No previous model hash found either.")
            global_model_hashes_by_round[round_id] = None
        print("Aggregator: Signaling clients to proceed (no new model).")
        for client_id in range(num_expected_clients):
            if client_id in client_aggregation_events:
                client_aggregation_events[client_id].set()
        return

    valid_state_dicts = []
    print("Aggregator: Processing received updates...")
    for client_id, model_hash in updates:
        try:
            state_dict = retrieve_data_by_hash(model_hash)
            for k, t in state_dict.items():
                state_dict[k] = torch.nan_to_num(t, nan=0.0, posinf=1e3, neginf=-1e3)
            if all(
                not torch.isnan(t).any() and not torch.isinf(t).any()
                for k, t in state_dict.items()
                if k.endswith('.weight') or k.endswith('.bias')
            ):
                valid_state_dicts.append(state_dict)
        except Exception:
            continue

    print(
        f"Aggregator: Validation complete. Valid models: {len(valid_state_dicts)}"
    )

    if not valid_state_dicts:
        print("Aggregator: No valid client models to aggregate for this round.")
        previous_hash = global_model_hashes_by_round.get(round_id - 1)
        global_model_hashes_by_round[round_id] = previous_hash
    else:
        global_state_dict = OrderedDict()
        first_valid_dict = valid_state_dicts[0]
        t0 = time.time()
        for key in first_valid_dict.keys():
            if first_valid_dict[key].is_floating_point():
                stacked = torch.stack(
                    [sd[key].cpu().float() for sd in valid_state_dicts], dim=0
                )
                global_state_dict[key] = stacked.mean(dim=0)
            else:
                global_state_dict[key] = first_valid_dict[key].cpu()
        global_model_hash = store_data_and_generate_hash(global_state_dict)
        logging.info(
            {
                "event": "timing",
                "component": "AggregatorCompute",
                "round": round_id,
                "num_models": len(valid_state_dicts),
                "duration_s": time.time() - t0,
            }
        )
        global_model_hashes_by_round[round_id] = global_model_hash
        print(
            f"Aggregator: Stored new global model hash {global_model_hash[:8]} for round {round_id}."
        )

    print("Aggregator: Signaling clients...")
    for client_id in range(num_expected_clients):
        client_aggregation_events[client_id].set()
    if round_id in client_updates_for_aggregation:
        del client_updates_for_aggregation[round_id]

    print(f"--- Aggregator: Finished Aggregation for Round {round_id} ---")
