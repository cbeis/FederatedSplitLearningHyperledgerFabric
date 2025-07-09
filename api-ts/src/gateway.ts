// src/gateway.ts
import * as grpc from '@grpc/grpc-js';
import {
  connect,
  Gateway,
  Contract,
  Network,
  Identity,
  Signer,
  signers,
} from '@hyperledger/fabric-gateway';
import { promises as fs } from 'fs';
import * as path from 'path';
import * as crypto from 'crypto';

let gateway: Gateway;
let network: Network;
let contract: Contract;
let client: grpc.Client;

async function getFirstFile(dir: string): Promise<string> {
  const files = await fs.readdir(dir);
  if (files.length === 0) throw new Error(`No files in ${dir}`);
  return path.join(dir, files[0]);
}

export async function initGateway(): Promise<void> {
  //
  // 1) Pick your org & peer from env (defaults to Org1 / peer0)
  //
  const orgName    = (process.env.ORG_NAME    || 'org1').toLowerCase();         // e.g. 'org2'
  const peerName   = (process.env.PEER_NAME   || 'peer0').toLowerCase();        // e.g. 'peer0'
  const orgDomain  = `${orgName}.example.com`;                                  // 'org2.example.com'
  const mspId      = process.env.MSP_ID      || `${orgName.charAt(0).toUpperCase()}${orgName.slice(1)}MSP`;
  const channel    = process.env.CHANNEL_NAME|| 'mychannel';
  const chaincode  = process.env.CHAINCODE_NAME|| 'fsl';

  //
  // 2) Derive all the paths & endpoints
  //
  const baseCrypto = path.resolve(
    __dirname,
    '../../../test-network/organizations/peerOrganizations',
    orgDomain
  );
  const tlsCertPath = path.join(baseCrypto, 'peers', `${peerName}.${orgDomain}`, 'tls', 'ca.crt');
  const peerEndpoint = process.env.PEER_ENDPOINT || (orgName === 'org1' ? 'localhost:7051' : 'localhost:9051');
  const peerHostAlias= process.env.PEER_HOST_ALIAS || `${peerName}.${orgDomain}`;

  const certDir = path.join(baseCrypto, 'users', `${process.env.USER_NAME || 'Admin'}@${orgDomain}`, 'msp', 'signcerts');
  const keyDir  = path.join(baseCrypto, 'users', `${process.env.USER_NAME || 'Admin'}@${orgDomain}`, 'msp', 'keystore');

  //
  // 3) Build one TLS+gRPC client
  //
  const tlscert = await fs.readFile(tlsCertPath);
  const creds   = grpc.credentials.createSsl(tlscert);
  client = new grpc.Client(peerEndpoint, creds, {
    'grpc.ssl_target_name_override': peerHostAlias,
  });

  //
  // 4) Load your identity & signer once
  //
  const certPath = await getFirstFile(certDir);
  const keyPath  = await getFirstFile(keyDir);
  const identity: Identity = {
    mspId,
    credentials: await fs.readFile(certPath),
  };
  const signer: Signer = signers.newPrivateKeySigner(
    crypto.createPrivateKey(await fs.readFile(keyPath))
  );

  //
  // 5) Connect gateway once, grab network & contract
  //
  gateway = connect({
    client,
    identity,
    signer,
    evaluateOptions:   () => ({ deadline: Date.now() + 5000 }),
    endorseOptions:    () => ({ deadline: Date.now() +15000 }),
    submitOptions:     () => ({ deadline: Date.now() + 5000 }),
    commitStatusOptions:()=> ({ deadline: Date.now() +60000 }),
  });
  network = gateway.getNetwork(channel);
  contract = network.getContract(chaincode);

  //
  // 6) Clean up on exit
  //
  const teardown = async () => {
    console.log('✂️ Closing gateway & gRPC channel');
    await gateway.close();
    client.close();
    process.exit(0);
  };
  process.on('SIGINT',  teardown);
  process.on('SIGTERM', teardown);
}

export function getContract(): Contract {
  if (!contract) throw new Error('Gateway not initialized');
  return contract;
}

export function getNetwork(): Network {
  if (!network) throw new Error('Gateway not initialized');
  return network;
}
