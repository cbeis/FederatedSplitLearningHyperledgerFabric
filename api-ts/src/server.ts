import {Express} from 'express'
import { WebSocketServer } from 'ws';
import bodyParser from 'body-parser';

import { initGateway, getContract, getNetwork } from './gateway';

const express = require('express');
const app: Express = express();
const port = parseInt(process.env.PORT || '3000', 10);

app.use(bodyParser.json({ limit: '300mb' }));
app.use(bodyParser.urlencoded({ limit: '300mb', extended: true }));

const wss = new WebSocketServer({ port: 8080, host: '0.0.0.0' });
let wsClient: any = null;
wss.on('connection', ws => {
  wsClient = ws;
  ws.on('close', () => {
    wsClient = null;
  });
});

app.post('/registerServer', async (req, res) => {
    const { topic } = req.body;
    try {
        const contract = await await getContract();
        await contract.submitTransaction('RegisterServer', topic); 
        res.send('Server registered.');
    } catch (error) {
        if (error instanceof Error){
            res.status(500).send(error.message);
        }
    }
});


app.post('/addGradients', async (req, res) => {
  const { clientBase64, data: cid } = req.body;
  try {
    const c = await getContract();
    await c.submit('AddGradients', {
      arguments:    [ clientBase64 ],
      transientData: { cid: Buffer.from(cid) }
    });
    console.log('✅ AddGradients was committed successfully');
    return res.json({ ok: true, cid });
  } catch (err: any) {
    return res.status(500).json({ ok: false, error: err.message });
  }
});


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

    if (ev.eventName.startsWith('IntermediateDataAdded:')) {
      const clientId = ev.eventName.split(':')[1];
      wsClient.send(JSON.stringify({
        event:     'IntermediateDataAdded',
        payload: {
          clientId,
          dataHash: payload.dataHash,
          txID:     payload.txID,
          mspID:    payload.mspID
        }
      }));
    }
  }

  events.close();
}

async function main() {
  await initGateway();

  app.listen(port, '0.0.0.0', () => {
    console.log(`🚀 Server listening on port ${port}`);
    listenForChaincodeEvents().catch(console.error);
  });
}

main().catch(err => {
  console.error('Failed to start server:', err);
  process.exit(1);
});





