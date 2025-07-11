import csv
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
import threading

import pandas as pd
import torch
from torch.utils.data import DataLoader, Subset
from torchvision import datasets, transforms

from client import Client
from config import BATCH_SIZE, EPOCHS, NUM_CLIENTS, global_commit_barrier, local_update_barrier
from evaluation import evaluate
from models import ClientModel, ServerModel
from server import Server

class JsonFormatter(logging.Formatter):
    def format(self, record):
        base = {
            "timestamp": record.created,
            "level":    record.levelname
        }
        if isinstance(record.msg, dict):
            base.update(record.msg)
        else:
            base["message"] = record.getMessage()
        return json.dumps(base)
class CsvHandler(logging.Handler):
    def __init__(self, path, fieldnames):
        super().__init__()
        self.file = open(path, "w", newline="")
        self.writer = csv.DictWriter(self.file, fieldnames=fieldnames)
        self.writer.writeheader()

    def emit(self, record):
        try:
            entry = json.loads(self.format(record))
            row = {k: entry.get(k, "") for k in self.writer.fieldnames}
            self.writer.writerow(row)
            self.file.flush()
        except Exception:
            pass

def save_results(epoch, epoch_time, accuracy, filepath):
    excel_file = Path(filepath)
    results = pd.DataFrame({'Epoch': [epoch], 'Time (seconds)': [epoch_time], 'Accuracy (%)': [accuracy]})
    if excel_file.exists():
        existing_results = pd.read_excel(excel_file)
        results = pd.concat([existing_results, results], ignore_index=True)
    results.to_excel(excel_file, index=False)

def setup_environment(num_clients: int, epochs: int, batch_size: int):
    server_url = [f"http://localhost:{port}" for port in range(3000, 3060)]
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    barrier = threading.Barrier(num_clients)

    stats = ((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010))
    transform_train = transforms.Compose([
        transforms.RandomCrop(32, padding=4, padding_mode="reflect"),
        transforms.RandomHorizontalFlip(),
        transforms.Resize(224),
        transforms.ToTensor(),
        transforms.Normalize(*stats),
    ])
    transform_test = transforms.Compose([
        transforms.Resize(224),
        transforms.ToTensor(),
        transforms.Normalize(*stats),
    ])

    train_dataset = datasets.CIFAR10(root="./data", train=True, download=True, transform=transform_train)
    test_dataset = datasets.CIFAR10(root="./data", train=False, download=True, transform=transform_test)

    client_datasets = [Subset(train_dataset, torch.arange(i, len(train_dataset), num_clients)) for i in range(num_clients)]
    train_loaders = [DataLoader(dataset, batch_size, shuffle=True) for dataset in client_datasets]
    test_loader = DataLoader(test_dataset, batch_size, shuffle=False)

    server_model = ServerModel(num_classes=10).to(device)
    base_ws_port = 8080
    server_websocket_url = f"ws://localhost:{base_ws_port}"
    server = Server(server_model, server_url[0], server_websocket_url, device)

    clients = []
    for i, train_loader in enumerate(train_loaders):
        ws_port = base_ws_port + (i + 1)
        client_websocket_url = f"ws://localhost:{ws_port}"
        model = ClientModel().to(device)
        client = Client(
            i,
            train_loader,
            test_loader,
            model,
            server,
            server_url[i + 1],
            client_websocket_url,
            device,
            barrier,
            epochs,
        )
        clients.append(client)

    return clients, server, test_loader


def main():
    num_clients = NUM_CLIENTS
    epochs = EPOCHS
    batch_size=BATCH_SIZE
    
    script_dir = Path(__file__).resolve().parent

    run_name = f"{num_clients}_{epochs}_{batch_size}"

    timestamp = datetime.now().strftime("%m%d_%H%M%S")
    results_dir = script_dir / f"results_fabric/{run_name}_{timestamp}"
    os.makedirs(results_dir, exist_ok=True)

    logger = logging.getLogger()
    for h in list(logger.handlers):
        logger.removeHandler(h)
    logger.setLevel(logging.INFO)
    fh = logging.FileHandler(f"{results_dir}/metrics.jsonl")
    fh.setFormatter(JsonFormatter())
    logging.getLogger().addHandler(fh)
    logs_dir = os.path.join( results_dir, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    csv_log_path = os.path.join(logs_dir, f"{run_name}_{timestamp}.csv")

    fieldnames = [
        "timestamp","level","event","epoch","test_accuracy","test_loss","duration_s",
        "component","client_id","batch","loss","size_bytes"
    ]
    csv_handler = CsvHandler(csv_log_path, fieldnames=fieldnames)
    csv_handler.setFormatter(JsonFormatter())
    logging.getLogger().addHandler(csv_handler)

    clients, server, test_loader = setup_environment(num_clients, epochs, batch_size)  # Adjust setup_environment to also return a common testloader

    server.start()
    time.sleep(1)
    for client in clients:
        client.start()
    
    for epoch in range(epochs):
        epoch_start_time = time.time()
        current_round_id = epoch + 1
        print(f"\n===== Starting Epoch {current_round_id}/{epochs} =====")

        print("Waiting for clients to complete local updates...")
        local_update_barrier.wait() 
        print("Waiting for clients to commit global hash…")
        global_commit_barrier.wait()
        print(f"Finalizing global model for round {current_round_id}")
        local_update_barrier.wait()

        if clients:
            accuracy, test_loss = evaluate(clients[0].device, clients[0].model, server.model, server.criterion, test_loader)
            epoch_duration = time.time() - epoch_start_time
            logging.info({
            "event":      "epoch_end",
            "epoch":      epoch,
            "duration_s": epoch_duration,
            "test_accuracy": accuracy,
            "test_loss":     test_loss
            })
            print(f"Accuracy after Epoch {current_round_id} aggregation: {accuracy:.2f}%")
            results_file = os.path.join(results_dir, "results.xlsx")
            save_results(current_round_id, epoch_duration, accuracy, filepath=results_file)


        print(f"===== End of Epoch {current_round_id}/{epochs} (Duration: {epoch_duration:.2f}s) =====")


    for epoch in range(epochs):
        for client in clients:
            client.join()
    
    server.stop()
    server.join()


if __name__ == "__main__":
    main()