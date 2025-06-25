from collections import OrderedDict
import csv
import pickle
import time, threading, torch, json
from pathlib import Path
import pandas as pd
import numpy as np
from queue import Queue, Empty
import websocket
import os
from datetime import datetime

from torchvision import transforms, datasets, models
import torch.nn as nn
import torch.optim as optim
from torch.optim.lr_scheduler import OneCycleLR
from torch.utils.data import DataLoader, Subset

import requests
from storage import store_data_and_generate_hash, retrieve_data_by_hash
import logging, sys, json

NUM_CLIENTS = 4
EPOCHS = 2
BATCH_SIZE = 16
global_model_hashes_by_round = {}
local_update_barrier = threading.Barrier(NUM_CLIENTS+1)
client_aggregation_events = {i: threading.Event() for i in range(NUM_CLIENTS)}
client_updates_for_aggregation = {}
global_client_outputs = [None] * NUM_CLIENTS
global_client_labels = [None] * NUM_CLIENTS
global_gradients = [None] * NUM_CLIENTS

class JsonFormatter(logging.Formatter):
    def format(self, record):
        base = {
            "timestamp": record.created,
            "level":    record.levelname
        }
        # if msg is a dict, merge it; else include as text
        if isinstance(record.msg, dict):
            base.update(record.msg)
        else:
            base["message"] = record.getMessage()
        return json.dumps(base)

class ClientModel(nn.Module):
    def __init__(self):
        super(ClientModel, self).__init__()
        resnet = models.resnet18(weights=None)
        self.features = nn.Sequential(*list(resnet.children())[:5])  # Retain layers up to the end of block 3

    def forward(self, x):
        x = self.features(x)
        return x

class ServerModel(nn.Module):
    def __init__(self, num_classes=10):
        super(ServerModel, self).__init__()
        resnet = models.resnet18(weights=None)
        self.rest_of_network = nn.Sequential(
            *list(resnet.children())[5:-1],
            nn.AdaptiveAvgPool2d((1, 1)),
            nn.Flatten()
        )
        self.fc = nn.Linear(resnet.fc.in_features, num_classes)

    def forward(self, x):
        x = self.rest_of_network(x)
        x = self.fc(x)
        return x

class Client(threading.Thread):
    def __init__(self, client_id, train_loader, test_loader, model, server, server_url, websocket_url, device, barrier, epochs):
        threading.Thread.__init__(self)
        self.client_id = client_id
        self.train_loader = train_loader
        self.test_loader = test_loader
        self.model = model
        self.server = server
        self.server_url = server_url
        self.websocket_url = websocket_url
        self.criterion = nn.CrossEntropyLoss()
        self.optimizer = optim.SGD(self.model.parameters(), lr=0.01, momentum=0.9, weight_decay=5e-4)
        self.scheduler = OneCycleLR(self.optimizer, max_lr=0.1, steps_per_epoch=len(train_loader), epochs=epochs)
        self.device = device
        self.barrier = barrier
        self.epochs = epochs
        self.gradients_ready = threading.Event()
        self.cid = None
    
    def timed_post(self, endpoint, **kwargs):
        t0 = time.time()
        url = f"{self.server_url}{endpoint}"
        resp = requests.post(url, **kwargs)
        dt = time.time() - t0
        logging.info({
          "event": "http_latency",
          "component": "Client",
          "endpoint": endpoint,
          "duration_s": dt,
          "client_id": self.client_id
        })
        return resp
    
    def timed_get(self, endpoint, **kwargs):
        t0 = time.time()
        url = f"{self.server_url}{endpoint}"
        resp = requests.get(url, **kwargs)
        dt = time.time() - t0
        logging.info({
          "event": "http_latency",
          "component": "Client",
          "endpoint": endpoint,
          "duration_s": dt,
          "client_id": self.client_id
        })
        return resp

    def start_websocket(self):
        self.ws = websocket.WebSocketApp(self.websocket_url,
                                        on_open=self.on_open,
                                        on_message=self.on_message,
                                        on_error=self.on_error,
                                        on_close=self.on_close)
        self.ws.run_forever()

    def on_message(self, ws, message):
        event = json.loads(message)
        self.cid = event['gradientData']
        self.gradients_ready.set()

    def on_error(self, ws, error):
        print(f"Client WebSocket error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        print(f"Client {self.client_id} WebSocket connection closed")

    def on_open(self, ws):
        print(f"Client {self.client_id} WebSocket connection opened")

    def run(self):
        websocket_thread = threading.Thread(target=self.start_websocket)
        websocket_thread.start()
        response = self.timed_get('/getServerAddress')
        server_address = json.loads(response.json()['serverAddress'])[0].split("server:")[1]
        response = self.timed_post('/registerClient', json={'serverAddress': server_address})
        data = response.json()
        fabric_id = data.get('clientId')
        print(f"[Fabric] Assigned clientId = {fabric_id} to client {self.client_id}")
        if response.status_code != 200:
            print(f"Client {self.client_id} already registered.")

        for epoch in range(self.epochs):
            self.model.train()
            self.barrier.wait()
            start_time = time.time()
            transact_time = 0.0
            print(f"Client {self.client_id} starting training in epoch {epoch}. \n" )
            iteration = 0
            for inputs, labels in self.train_loader:
                iteration += 1
                if self.client_id == 0: print(time.time() - transact_time)
                transact_time = time.time()
                if  self.client_id == 0 and iteration%10 == 0:print(iteration)
                inputs, labels = inputs.to(self.device), labels.to(self.device)
                # Forward pass on client
                self.optimizer.zero_grad()
                t0 = time.time()
                client_outputs = self.model(inputs)
                logging.info({
                    "event": "timing",
                    "component": "ClientFwd",
                    "epoch": epoch,
                    "client_id": self.client_id,
                    "duration_s": time.time() - t0
                    })
                payload = pickle.dumps((client_outputs.clone().cpu(),labels.cpu()))
                logging.info({
                    "event":"comm_volume",
                    "component":"ClientUploadAct",
                    "epoch":epoch,
                    "client_id":self.client_id,
                    "size_bytes":len(payload)
                    })
                intermediate_data =store_data_and_generate_hash((client_outputs.clone().cpu(),labels.cpu()))
                transact_time2 = 0.0
                initial_time = time.time()
                self.timed_post('/addIntermediateData', json={'data': intermediate_data})
                transact_time2 = time.time() -initial_time

                while not self.gradients_ready.is_set():
                    self.gradients_ready.wait()
                self.gradients_ready.clear()
                server_loss = retrieve_data_by_hash(self.cid)
                server_loss = server_loss.to(self.device)
                t0 = time.time()
                client_outputs.backward(gradient=server_loss)
                nn.utils.clip_grad_norm_(self.model.parameters(), max_norm=1)  # Gradient clipping
                self.optimizer.step()
                self.scheduler.step()
                duration = time.time() - t0
                logging.info({
                    "event": "timing",
                    "component": "ClientBwd",
                    "epoch": epoch,
                    "client_id": self.client_id,
                    "duration_s": duration
                    })
                torch.cuda.empty_cache()

            epoch_time = time.time() - start_time
            print(f"Client {self.client_id} finished training epoch in: " + str(epoch_time))

            full_sd = self.model.state_dict()
            param_sd = {k: v.cpu().clone() for k, v in full_sd.items() if k.endswith('.weight') or k.endswith('.bias')}
            client_model_hash = store_data_and_generate_hash(param_sd)
            payload = pickle.dumps(param_sd)
            logging.info({
            "event":"comm_volume",
            "component":"ClientUploadModel",
            "epoch": epoch,
            "client_id": self.client_id,
            "size_bytes": len(payload)
            })
            round_id = epoch + 1
            client_updates_for_aggregation.setdefault(round_id, []).append((self.client_id, client_model_hash))

            local_update_barrier.wait()
            client_aggregation_events[self.client_id].clear()
            client_aggregation_events[self.client_id].wait()
            global_model_hash = global_model_hashes_by_round.get(round_id)
            if global_model_hash:
                try:
                    global_model_state = retrieve_data_by_hash(global_model_hash)
                    payload = pickle.dumps(global_model_state)
                    logging.info({
                    "event":"comm_volume",
                    "component":"ClientDownloadModel",
                    "epoch": epoch,
                    "client_id": self.client_id,
                    "size_bytes": len(payload)
                    })
                    self.model.load_state_dict(global_model_state, strict= False)
                except Exception:
                    pass

class Server(threading.Thread):
    def __init__(self, model, server_url, websocket_url, device, transact_time=0.0):
        threading.Thread.__init__(self)
        self.server_url = server_url
        self.websocket_url = websocket_url
        self.device = device
        self.model = model
        self.optimizer = optim.SGD(self.model.parameters(), lr=0.01, momentum=0.9, weight_decay=5e-4)
        self.scheduler = OneCycleLR(self.optimizer, max_lr=0.1, steps_per_epoch=50000, epochs=50)
        self.criterion = nn.CrossEntropyLoss()
        self.lock = threading.Lock()
        self.task_queue = Queue()
        self.stop_event = threading.Event()
        self.transact_time = 0.0

    def timed_post(self, endpoint, **kwargs):
        url = f"{self.server_url}{endpoint}"
        t0 = time.time()
        resp = requests.post(url, **kwargs)
        logging.info({
        "event":"http_latency",
        "endpoint": endpoint,
        "duration_s": time.time()-t0,
        })
        return resp

    def run(self):
        response = self.timed_post('/registerServer', json={'topic': 'health'})
        websocket_thread = threading.Thread(target=self.start_websocket)
        websocket_thread.start()

        if response.status_code != 200:
            print("Server already registered.")

        while not self.stop_event.is_set():
            try:
                client_address, intermediate_data = self.task_queue.get(timeout=0.001)  # Timeout to check for stop_event
                #client_outputs, labels = intermediate_data
                task_thread = threading.Thread(target=self.process_client_data, args=(intermediate_data, client_address))
                task_thread.start()
            except Empty:
                continue
    
    def stop(self):
        self.stop_event.set()

    def start_websocket(self):
        self.ws = websocket.WebSocketApp(self.websocket_url,
                                        on_open=self.on_open,
                                        on_message=self.on_message,
                                        on_error=self.on_error,
                                        on_close=self.on_close)
        self.ws.run_forever()

    def on_message(self, ws, message):
        #print("⇨ [WS raw] ", message)
        msg = json.loads(message)
        pl = msg['payload']
        ev = msg.get('event')
        cid = pl.get('intermediateData')
        client_id = pl.get('clientId')
        self.task_queue.put((client_id, cid))

    def on_error(self, ws, error):
        print(f"Server WebSocket error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        print(f"Server WebSocket connection closed")

    def on_open(self, ws):
        print(f"Server WebSocket connection opened")

    def process_client_data(self, intermediate_data, client_address):
        t0 = time.time()
        with self.lock:
            self.model.train()
            data = retrieve_data_by_hash(intermediate_data)
            #del ifpsDB[intermediate_data]
            client_outputs, labels = data[0],data[1]
            client_outputs = client_outputs.to(self.device)
            labels = labels.to(self.device)
            self.optimizer.zero_grad()
            client_outputs = client_outputs.detach().requires_grad_()
            server_outputs = self.model(client_outputs)
            loss = self.criterion(server_outputs, labels)
            logging.info({
                "event":     "server_train_loss",
                "client_id": client_address,
                "loss":      loss.item(),
            })
            loss.backward()
            self.optimizer.step()
            self.scheduler.step()
        duration = time.time() - t0
        logging.info({
            "event": "timing",
            "component": "ServerBatch",
            "client_id": client_address,
            "duration_s": duration
            })
        gradient_hash = store_data_and_generate_hash(client_outputs.grad.cpu())
        torch.cuda.empty_cache()
        start_time = time.time()
        transact_time2 = 0.0
        self.timed_post('/addGradients', json={'clientBase64': client_address, 'data': gradient_hash})
        transact_time2 = time.time() - start_time
        self.transact_time = self.transact_time + (time.time() - start_time)
        return 


def evaluate(device, client_model, server_model, criterion, testloader):
    client_model.eval()
    server_model.eval()
    correct = 0
    total = 0
    total_loss = 0.0
    with torch.no_grad():
        for images, labels in testloader:
            images, labels = images.to(device), labels.to(device)
            
            # Forward pass through client model
            client_outputs = client_model(images)
            
            # Forward pass through server model
            outputs = server_model(client_outputs)
            loss = criterion(outputs, labels)
            total_loss += loss.item() * labels.size(0)
            _, predicted = torch.max(outputs.data, 1)
            total += labels.size(0)
            correct += (predicted == labels).sum().item()
    test_loss = total_loss / total
    return (100 * correct / total), test_loss

def save_results(epoch, epoch_time, accuracy, filepath):
    excel_file = Path(filepath)
    results = pd.DataFrame({'Epoch': [epoch], 'Time (seconds)': [epoch_time], 'Accuracy (%)': [accuracy]})
    if excel_file.exists():
        existing_results = pd.read_excel(excel_file)
        results = pd.concat([existing_results, results], ignore_index=True)
    results.to_excel(excel_file, index=False)

def perform_aggregation(round_id, num_expected_clients):

    print(f"\n--- Aggregator: Starting Aggregation for Round {round_id} ---")
    updates = client_updates_for_aggregation.get(round_id, [])

    if not updates:
        print("Aggregator: No client updates received for this round.")
        previous_hash = global_model_hashes_by_round.get(round_id - 1)
        if previous_hash:
            global_model_hashes_by_round[round_id] = previous_hash
            print(f"Aggregator: Carrying over model hash from round {round_id-1}.")
        else:
            print("Aggregator: WARNING - No previous model hash found either.")
            global_model_hashes_by_round[round_id] = None 
        print("Aggregator: Signaling clients to proceed (no new model).")
        for client_id in range(num_expected_clients):
            if client_id in client_aggregation_events:
                client_aggregation_events[client_id].set()
        return 

    valid_state_dicts = []
    retrieval_errors = 0
    invalid_models = 0
    processed_hashes = set()

    print(f"Aggregator: Processing received updates...")
    for client_id, model_hash in updates:
        try:
            state_dict = retrieve_data_by_hash(model_hash)
            for k, t in state_dict.items(): state_dict[k] = torch.nan_to_num(t, nan=0.0, posinf=1e3, neginf=-1e3)
            if all(not torch.isnan(t).any() and not torch.isinf(t).any() for k, t in state_dict.items() if k.endswith('.weight') or k.endswith('.bias')):
                valid_state_dicts.append(state_dict)

        except:
            continue

    print(f"Aggregator: Validation complete. Valid models: {len(valid_state_dicts)}, Invalid models (NaN/Inf): {invalid_models}, Retrieval errors: {retrieval_errors}")

    if not valid_state_dicts:
        print("Aggregator: No valid client models to aggregate for this round.")
        previous_hash = global_model_hashes_by_round.get(round_id - 1)
        global_model_hashes_by_round[round_id] = previous_hash
        print(f"Aggregator: Carrying over model hash from round {round_id-1}.")
    else:
        global_state_dict = OrderedDict(); first_valid_dict = valid_state_dicts[0]; num_aggregated = len(valid_state_dicts)
        print(f"Aggregator: Averaging {num_aggregated} valid models...")
        t0 = time.time()        
        for key in first_valid_dict.keys():
            if first_valid_dict[key].is_floating_point():
                    stacked_tensors = torch.stack([sd[key].cpu().float() for sd in valid_state_dicts], dim=0)
                    global_state_dict[key] = stacked_tensors.mean(dim=0)
            else: global_state_dict[key] = first_valid_dict[key].cpu()

        global_model_hash = store_data_and_generate_hash(global_state_dict)
        duration = time.time() - t0
        logging.info({
            "event": "timing",
            "component": "AggregatorCompute",
            "round": round_id,
            "num_models": len(valid_state_dicts),
            "duration_s": duration
            })
        global_model_hashes_by_round[round_id] = global_model_hash
        print(f"Aggregator: Stored new global model hash {global_model_hash[:8]} for round {round_id}.")

    print("Aggregator: Signaling clients...")
    for client_id in range(num_expected_clients): 
        client_aggregation_events[client_id].set()
    if round_id in client_updates_for_aggregation:
        del client_updates_for_aggregation[round_id]

    print(f"--- Aggregator: Finished Aggregation for Round {round_id} ---")

def setup_environment(num_clients, epochs, batch_size):
    server_url = [f"http://localhost:{port}" for port in range(3000, 3060)]
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    barrier = threading.Barrier(num_clients)

    # Define transformations and load datasets
    stats = ((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010))
    transform_train = transforms.Compose([
    transforms.RandomCrop(32, padding=4,padding_mode='reflect'),
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
    
    train_dataset = datasets.CIFAR10(root='./data', train=True, download=True, transform=transform_train)
    test_dataset = datasets.CIFAR10(root='./data', train=False, download=True, transform=transform_test)
    
    # Split dataset among clients
    client_datasets = [Subset(train_dataset, np.arange(i, len(train_dataset), num_clients)) for i in range(num_clients)]
    train_loaders = [DataLoader(dataset, batch_size, shuffle=True) for dataset in client_datasets]
    test_loader = DataLoader(test_dataset, batch_size, shuffle=False) 

    server_model = ServerModel(num_classes=10).to(device)
    base_ws_port = 8080
    server_websocket_url = f"ws://localhost:{base_ws_port}"
    server = Server(server_model, server_url[0],server_websocket_url, device)

    clients = []
    for i, train_loader in enumerate(train_loaders):
        ws_port = base_ws_port + (i + 1) 
        client_websocket_url = f"ws://localhost:{ws_port}"
        model = ClientModel().to(device)
        client = Client(i, train_loader, test_loader, model, server, server_url[i+1], client_websocket_url, device, barrier, epochs)
        clients.append(client)


    return clients, server, test_loader

def main():
    num_clients = NUM_CLIENTS
    epochs = EPOCHS
    batch_size=BATCH_SIZE
    
    script_dir = Path(__file__).resolve().parent

    run_name = f"EXP3_{num_clients}_{epochs}_{batch_size}"

    timestamp = datetime.now().strftime("%m%d_%H%M%S")
    results_dir = script_dir / f"results_fabric/{run_name}_{timestamp}"
    os.makedirs(results_dir, exist_ok=True)

    logger = logging.getLogger()
    for h in list(logger.handlers):
        logger.removeHandler(h)
    logger.setLevel(logging.INFO)

    # file handler for JSON-lines metrics
    fh = logging.FileHandler(f"{results_dir}/metrics.jsonl")
    fh.setFormatter(JsonFormatter())
    logging.getLogger().addHandler(fh)

    # 1) Create the logs folder under results_fabric
    logs_dir = os.path.join( results_dir, "logs")
    os.makedirs(logs_dir, exist_ok=True)

    # 2) Path for the CSV log
    csv_log_path = os.path.join(logs_dir, f"{run_name}_{timestamp}.csv")

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

    # 4) Define the columns you log in logging.info({...})
    fieldnames = [
        "timestamp","level","event","epoch","test_accuracy","test_loss","duration_s",
        "component","client_id","batch","loss","size_bytes"
    ]

    # 5) Attach the handler
    csv_handler = CsvHandler(csv_log_path, fieldnames=fieldnames)
    csv_handler.setFormatter(JsonFormatter())
    logging.getLogger().addHandler(csv_handler)


    clients, server, test_loader = setup_environment(num_clients, epochs, batch_size)  # Adjust setup_environment to also return a common testloader

    server.start()
    for client in clients:
        client.start()
    
    for epoch in range(epochs):
        epoch_start_time = time.time()
        current_round_id = epoch + 1
        print(f"\n===== Orchestrator: Starting Epoch {current_round_id}/{epochs} =====")

        print("Orchestrator: Waiting for clients to complete local updates...")
        local_update_barrier.wait() 


        perform_aggregation(current_round_id, num_clients)

        time.sleep(0.5) 
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
            print(f"Orchestrator: Accuracy after Epoch {current_round_id} aggregation: {accuracy:.2f}%")
            results_file = os.path.join(results_dir, "results.xlsx")
            save_results(current_round_id, epoch_duration, accuracy, filepath=results_file)


        print(f"===== Orchestrator: End of Epoch {current_round_id}/{epochs} (Duration: {epoch_duration:.2f}s) =====")


    for epoch in range(epochs):
        for client in clients:
            client.join()
    
    server.stop()
    server.join()

if __name__ == '__main__':
    main()