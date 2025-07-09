from collections import OrderedDict
import hashlib
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
from config import (
    global_commit_barrier,
    local_update_barrier,
)

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
        self.aggregation_task_ready = threading.Event()
        self.global_model_ready = threading.Event()
        self.aggregation_payload = None
        self.final_global_hash = None             
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
        msg = json.loads(message)
        ev        = msg.get('event')
        payload = msg.get('payload', {})
        if ev == 'GradientsAdded':
            data_hash = payload.get('dataHash')
            self.cid = data_hash
            self.gradients_ready.set()
        elif ev == 'AggregationTaskStart':
            updates = payload.get('updates', []) 
            self.aggregation_payload = updates
            self.aggregation_task_ready.set()
        elif ev == 'GlobalModelUpdated':
            self.final_global_hash = msg.get('payload')['consensusHash']
            self.global_model_ready.set()

    def on_error(self, ws, error):
        print(f"Client WebSocket error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        print(f"Client {self.client_id} WebSocket connection closed")

    def on_open(self, ws):
        print(f"Client {self.client_id} WebSocket connection opened")

    def stable_serialize(self, state: OrderedDict[str, torch.Tensor]) -> bytes:
        """
        Turn global_state into a canonical byte sequence:
        keys sorted, tensors raw‐byted in a fixed order.
        """
        buf = bytearray()
        for key in state.keys():               
            buf.extend(key.encode('utf-8'))    
            buf.extend(b"\0")                  
            buf.extend(state[key].cpu().numpy().tobytes())
        return bytes(buf)

    def compute_hash(self, state: OrderedDict[str, torch.Tensor]) -> str:
        h = hashlib.sha256()
        h.update(self.stable_serialize(state))
        return h.hexdigest()

    def _perform_local_aggregation(self, updates: list[dict]):
        """
        Given a list of client updates (each with 'ModelParamHash' and 'DatasetSize'),
        fetch each model, average them (weighted by DatasetSize), store the new state,
        then commit via chaincode.
        """
        # 1) Fetch & validate all state dicts
        state_dicts, total_size, current_round = [], 0, 0
        for upd in updates:
            h = upd['modelParamHash']
            size = upd['datasetSize']
            size = int(size)
            current_round = upd['roundID']
            sd = retrieve_data_by_hash(h)
            state_dicts.append((sd, size))
            total_size += size

        # 2) Weighted average
        global_state: OrderedDict[str, torch.Tensor] = OrderedDict()
        for key in state_dicts[0][0].keys():
            # Stack and weight
            stacked = torch.stack([
                sd[key].cpu().float() * (size / total_size)
                for sd, size in state_dicts
            ], dim=0)
            global_state[key] = stacked.sum(dim=0)

        pure_hash = self.compute_hash(global_state)  
        # 3) Store & get hash
        aggregated_hash = store_data_and_generate_hash(global_state)
        logging.info({
            "event":      "local_aggregate",
            "round":      current_round,
            "client_id":  self.client_id,
            "agg_hash":   pure_hash,
            "cid_hash":   aggregated_hash
        })

        # 4) Commit aggregated hash
        resp = self.timed_post(
            '/commitGlobalModelHash',
            json={
                'roundID':            current_round,
                'aggregatedGlobalModelHash': pure_hash,
                'cid': aggregated_hash
            }
        )
        resp.raise_for_status()

        # 5) Signal orchestrator barrier
        global_commit_barrier.wait()

    def _load_global_model(self, consensus_hash: str):
        """
        Upon receiving the final consensus hash, fetch the model and load it.
        """
        state = retrieve_data_by_hash(consensus_hash)
        self.model.load_state_dict(state, strict=False)
        logging.info({
            "event":      "load_global_model",
            "round":      self.current_round,
            "client_id":  self.client_id,
            "hash":       consensus_hash
        })

    def run(self):
        websocket_thread = threading.Thread(target=self.start_websocket)
        websocket_thread.start()
        response = self.timed_get('/getServerAddress')
        server_address = json.loads(response.json()['serverAddress'])[0].split("server:")[1]
        response = self.timed_post('/registerClient', json={'serverAddress': server_address})
        if response.status_code != 200:
            print(f"Client {self.client_id} already registered.")

        for epoch in range(self.epochs):
            self.model.train()
            self.barrier.wait()
            start_time = time.time()
            print(f"Client {self.client_id} starting training in epoch {epoch}. \n" )
            iteration = 0
            for inputs, labels in self.train_loader:
                iteration += 1
                if iteration == 3:
                    break
                inputs, labels = inputs.to(self.device), labels.to(self.device)

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

                self.timed_post('/addIntermediateData', json={'data': intermediate_data})

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
            round_id = str(epoch + 1)
            dataset_size = len(self.train_loader.dataset)
            
            print(f"Client {self.client_id} submitting model hash for round {round_id}...")
            
            self.timed_post('/submitClientModelHash', json={
                'roundID': round_id,
                'modelParamHash': client_model_hash,
                'datasetSize': dataset_size
            })

            local_update_barrier.wait() 
            
            if self.client_id == 0:
                print(f" Triggering AggregationTaskStart for round {round_id}")
                resp = requests.post(f"http://localhost:{3001}/triggerClientAggregation", json={'roundID': round_id})
                resp.raise_for_status()

            print(f"Client {self.client_id} waiting for aggregation task...")
            self.aggregation_task_ready.wait()
            self.aggregation_task_ready.clear()

            self._perform_local_aggregation(self.aggregation_payload)

            if self.client_id == 0:
                print("Client 0 waiting a moment before triggering finalization...")
                time.sleep(5) 
                print("Client 0 triggering EndGlobalModel...")
                self.timed_post('/endGlobalModel', json={'roundID': round_id})

            print(f"Client {self.client_id} waiting for final global model...")
            self.global_model_ready.wait()
            self.global_model_ready.clear()
            
            if self.final_global_hash:
                try:
                    print(f"Client {self.client_id} loading final global model {self.final_global_hash[:8]}...")
                    global_model_state = retrieve_data_by_hash(self.final_global_hash)
                    self.model.load_state_dict(global_model_state, strict=False)
                except Exception as e:
                    print(f"Client {self.client_id} failed to load final model: {e}")
            
            local_update_barrier.wait()
            self.final_global_hash = None 
