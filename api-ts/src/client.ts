import { Express } from 'express';
import { TextDecoder } from 'util';
import { WebSocketServer } from 'ws';
import bodyParser from 'body-parser';
import { initGateway, getContract, getNetwork } from './gateway';

const utf8Decoder = new TextDecoder();
const express = require('express');
const app: Express = express();
const port = parseInt(process.env.PORT || '3000', 10);
const wsPort = parseInt(process.env.WS_PORT || '8080', 10);

app.use(bodyParser.json({ limit: '300mb' }));
app.use(bodyParser.urlencoded({ limit: '300mb', extended: true }));

// WS to receive events
const wss = new WebSocketServer({ port: wsPort, host: '0.0.0.0' });
let wsClient: any = null;
let clientID: string | null = null;
wss.on('connection', ws => {
  wsClient = ws;
  ws.on('close', () => (wsClient = null));
});

app.get('/getServerAddress', async (req, res) => {
    try {
        const contract = await getContract();
        const serverAddress = await contract.evaluateTransaction('GetServerAddress');
        const decodedAddress = utf8Decoder.decode(serverAddress); // Decode Uint8Array to string
        res.json({ serverAddress: decodedAddress });
    } catch (error) {
        if (error instanceof Error) {
            res.status(500).send(error.message);
        }
    }
});

app.post('/registerClient', async (req, res) => {
    const { serverAddress } = req.body;
    try {
        const contract = getContract();
        const clientIdBuffer = await contract.submitTransaction('RegisterClient', serverAddress);
        clientID = utf8Decoder.decode(clientIdBuffer); 
        res.json({
            message: 'Client registered.',
            clientId: clientID
          });
    } catch (error) {
        if (error instanceof Error){
            res.status(500).send(error.message);
        }
    }
});

app.post('/addIntermediateData', async (req, res) => {
  const { data: cid } = req.body;            
  try {
    const contract = getContract();
    await contract.submit('AddIntermediateData', { transientData: { 'cid': Buffer.from(cid) }  });
    console.log('✅ AddIntermediateData was committed successfully');
    return res.json({ ok: true, cid });
  } catch (error: any) {
    return res.status(500).json({ ok: false, error: error.message });
  }
});

app.post('/submitClientModelHash', async (req, res) => {
    const { roundID, modelParamHash, datasetSize } = req.body;
    console.log(`🔑 Submitting modelParamHash for round ${roundID}:`, modelParamHash);
    try {
        const c = getContract();
        await c.submit('SubmitClientModelHash', {
          arguments:    [ roundID, String(datasetSize)],
          transientData: { modelHash: Buffer.from(modelParamHash) }
        });
        return res.json({ success: true, message: `Hash for round ${roundID} submitted.` });
    } catch (error) {
        if (error instanceof Error) {
            res.status(500).send(error.message);
        }
    }
});

app.post('/triggerClientAggregation', async (req, res) => {
    const { roundID } = req.body;
    console.log(`📡 [triggerClientAggregation] req.body =`, req.body);
    try {
        const contract = getContract();
        const result = await contract.submitTransaction('TriggerClientAggregation', String(roundID));
        return res.json({ success: true, message: `Aggregation triggered for round ${roundID}.` });
    } catch (error) {
        if (error instanceof Error) {
            console.error(`❌ [triggerClientAggregation] error:`, error);
            res.status(500).send(error.message);
        }
    }
});

app.post('/commitGlobalModelHash', async (req, res) => {
    const { roundID, aggregatedGlobalModelHash, cid } = req.body;
    try {
        const contract = getContract();
        await contract.submitTransaction('CommitGlobalModelHash', roundID, aggregatedGlobalModelHash, cid);
        res.json({ success: true, message: `Global model hash committed for round ${roundID}.` });
    } catch (error) {
        if (error instanceof Error) {
            res.status(500).send(error.message);
        }
    }
});

app.post('/endGlobalModel', async (req, res) => {
  const { roundID } = req.body;
  const rid = String(roundID);

  console.log(`📡 [endGlobalModel] invoking for round ${rid}`);

  try {
    const contract = getContract();
    await contract.submitTransaction('EndGlobalModel', rid);
    console.log(`✅ [endGlobalModel] success for round ${rid}`);
    return res.json({
      success: true,
      message: `Global model finalized for round ${rid}.`
    });
  } catch (error) {
    // fallback
    console.error('❌ [endGlobalModel] error:', error);
    return res
      .status(500)
      .send(error instanceof Error ? error.message : JSON.stringify(error));
  }
});

// listen for GradientsAdded events
async function listenForChaincodeEvents() {
  const network    = getNetwork();
  const ccName     = process.env.CHAINCODE_NAME || 'fsl';
  const events     = await network.getChaincodeEvents(ccName);

  for await (const ev of events) {
    const raw       = Buffer.from(ev.payload).toString('utf8');
    let payload: any = {};
    try { payload = JSON.parse(raw) } catch { /* ignore */ }

    if (!wsClient || wsClient.readyState !== wsClient.OPEN) {
      continue;
    }

    if (
      ev.eventName.startsWith('GradientsAdded:')
    ) {
      wsClient.send(JSON.stringify({
        event:     'GradientsAdded',
        payload: {
          clientId: clientID,
          dataHash: payload.dataHash,
          txID:     payload.txID,
          mspID:    payload.mspID
        }
      }));
    }

    // Aggregator says “here’s the list of client model updates to aggregate”
    if (ev.eventName.startsWith('AggregationTaskStart:')) {
      // decode the raw Fabric payload bytes into a string…
      const raw = utf8Decoder.decode(ev.payload);
      let parsed;
      try {
        parsed = JSON.parse(raw);   // { roundID: "...", updates: [...] }
      } catch (err) {
        console.error('⛔️ Failed to parse AggregationTaskStart payload:', raw);
        return;
      }

      // fire your WS event with the full parsed struct
      wsClient.send(JSON.stringify({
        event:   'AggregationTaskStart',
        payload: parsed            // has .roundID (string) and .updates (ClientModelUpdate[])
      }));
    }

    // Final global‐model hash published 
    if (ev.eventName.startsWith('GlobalModelUpdated:')) {
      const roundID     = ev.eventName.split(':')[1];
      const consensus   = raw;  // this event’s payload is just the hash string
      wsClient.send(JSON.stringify({
        event:   'GlobalModelUpdated',
        payload: { roundID, consensusHash: consensus }
      }));
    }
  }

  events.close();
}

async function main() {
  await initGateway();

  app.listen(port, '0.0.0.0', () => {
    console.log(`🤖 Client TS-API listening on port ${port}`);
    listenForChaincodeEvents().catch(console.error);
  });
}

main().catch(err => {
  console.error('Client failed to start:', err);
  process.exit(1);
});




