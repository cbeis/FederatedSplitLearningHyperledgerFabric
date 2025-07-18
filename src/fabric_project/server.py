import json
import logging
import threading
import time
from queue import Queue, Empty

import requests
import torch
import torch.nn as nn
import torch.optim as optim
from torch.optim.lr_scheduler import OneCycleLR
import websocket

from storage import store_data_and_generate_hash, retrieve_data_by_hash

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
   
    def stop(self):
        self.stop_event.set()

    def start_websocket(self):
        self.ws = websocket.WebSocketApp(self.websocket_url,
                                        on_open=self.on_open,
                                        on_message=self.on_message,
                                        on_error=self.on_error,
                                        on_close=self.on_close)
        self.ws.run_forever()

    def on_message(self, ws, raw_msg):
        msg = json.loads(raw_msg)
        ev = msg.get('event')
        payload = msg.get('payload', {})
        if ev == 'IntermediateDataAdded':
            client_id     = payload.get('clientId')
            data_hash  = payload.get('dataHash')
            if not data_hash:
                print(f"[WARN] empty intermediateData for client {client_id}, dropping")
                return
            self.task_queue.put((client_id, data_hash))

    def on_error(self, ws, error):
        print(f"Server WebSocket error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        print(f"Server WebSocket connection closed")

    def on_open(self, ws):
        print(f"Server WebSocket connection opened")

    def run(self):
        response = self.timed_post('/registerServer', json={'topic': 'health'})
        websocket_thread = threading.Thread(target=self.start_websocket)
        websocket_thread.start()

        if response.status_code != 200:
            print("Server already registered.")

        while not self.stop_event.is_set():
            try:
                client_address, intermediate_data = self.task_queue.get(timeout=0.001)  
                task_thread = threading.Thread(target=self.process_client_data, args=(intermediate_data, client_address))
                task_thread.start()
            except Empty:
                continue
 
    def process_client_data(self, data_hash, client_address):
        t0 = time.time()

        with self.lock:
            self.model.train()
            data = retrieve_data_by_hash(data_hash)
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
        self.timed_post('/addGradients', json={'clientBase64': client_address, 'data': gradient_hash})
        self.transact_time = self.transact_time + (time.time() - start_time)
        return 
