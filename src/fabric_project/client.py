import json
import logging
import pickle
import threading
import time

import requests
import torch
import torch.nn as nn
import torch.optim as optim
from torch.optim.lr_scheduler import OneCycleLR
import websocket

from storage import store_data_and_generate_hash, retrieve_data_by_hash
from aggregation import (
    local_update_barrier,
    client_aggregation_events,
    client_updates_for_aggregation,
    global_model_hashes_by_round,
)


class Client(threading.Thread):
    def __init__(
        self,
        client_id: int,
        train_loader,
        test_loader,
        model,
        server,
        server_url: str,
        websocket_url: str,
        device,
        barrier,
        epochs: int,
    ) -> None:
        super().__init__()
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
        logging.info(
            {
                "event": "http_latency",
                "component": "Client",
                "endpoint": endpoint,
                "duration_s": dt,
                "client_id": self.client_id,
            }
        )
        return resp

    def timed_get(self, endpoint, **kwargs):
        t0 = time.time()
        url = f"{self.server_url}{endpoint}"
        resp = requests.get(url, **kwargs)
        dt = time.time() - t0
        logging.info(
            {
                "event": "http_latency",
                "component": "Client",
                "endpoint": endpoint,
                "duration_s": dt,
                "client_id": self.client_id,
            }
        )
        return resp

    def start_websocket(self):
        self.ws = websocket.WebSocketApp(
            self.websocket_url,
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
        )
        self.ws.run_forever()

    def on_message(self, ws, message):
        event = json.loads(message)
        self.cid = event["gradientData"]
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
        response = self.timed_get("/getServerAddress")
        server_address = json.loads(response.json()["serverAddress"])[0].split("server:")[1]
        response = self.timed_post("/registerClient", json={"serverAddress": server_address})
        data = response.json()
        fabric_id = data.get("clientId")
        print(f"[Fabric] Assigned clientId = {fabric_id} to client {self.client_id}")
        if response.status_code != 200:
            print(f"Client {self.client_id} already registered.")

        for epoch in range(self.epochs):
            self.model.train()
            self.barrier.wait()
            start_time = time.time()
            print(f"Client {self.client_id} starting training in epoch {epoch}.\n")
            for inputs, labels in self.train_loader:
                inputs, labels = inputs.to(self.device), labels.to(self.device)
                self.optimizer.zero_grad()
                t0 = time.time()
                client_outputs = self.model(inputs)
                logging.info(
                    {
                        "event": "timing",
                        "component": "ClientFwd",
                        "epoch": epoch,
                        "client_id": self.client_id,
                        "duration_s": time.time() - t0,
                    }
                )
                payload = pickle.dumps((client_outputs.clone().cpu(), labels.cpu()))
                logging.info(
                    {
                        "event": "comm_volume",
                        "component": "ClientUploadAct",
                        "epoch": epoch,
                        "client_id": self.client_id,
                        "size_bytes": len(payload),
                    }
                )
                intermediate_data = store_data_and_generate_hash((client_outputs.clone().cpu(), labels.cpu()))
                self.timed_post("/addIntermediateData", json={"data": intermediate_data})

                while not self.gradients_ready.is_set():
                    self.gradients_ready.wait()
                self.gradients_ready.clear()
                server_loss = retrieve_data_by_hash(self.cid)
                server_loss = server_loss.to(self.device)
                t0 = time.time()
                client_outputs.backward(gradient=server_loss)
                nn.utils.clip_grad_norm_(self.model.parameters(), max_norm=1)
                self.optimizer.step()
                self.scheduler.step()
                logging.info(
                    {
                        "event": "timing",
                        "component": "ClientBwd",
                        "epoch": epoch,
                        "client_id": self.client_id,
                        "duration_s": time.time() - t0,
                    }
                )
                torch.cuda.empty_cache()

            epoch_time = time.time() - start_time
            print(f"Client {self.client_id} finished training epoch in: {epoch_time}")

            full_sd = self.model.state_dict()
            param_sd = {k: v.cpu().clone() for k, v in full_sd.items() if k.endswith('.weight') or k.endswith('.bias')}
            client_model_hash = store_data_and_generate_hash(param_sd)
            payload = pickle.dumps(param_sd)
            logging.info(
                {
                    "event": "comm_volume",
                    "component": "ClientUploadModel",
                    "epoch": epoch,
                    "client_id": self.client_id,
                    "size_bytes": len(payload),
                }
            )
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
                    logging.info(
                        {
                            "event": "comm_volume",
                            "component": "ClientDownloadModel",
                            "epoch": epoch,
                            "client_id": self.client_id,
                            "size_bytes": len(payload),
                        }
                    )
                    self.model.load_state_dict(global_model_state, strict=False)
                except Exception:
                    pass
