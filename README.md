# HLF-FSL: Hyperledger Fabric Federated Split Learning

Decentralized Federated Split Learning (FSL) on Hyperledger Fabric using transient fields, Private Data Collections, and off-chain storage for large parameters.

Based on: Beis-Penedo et al., “HLF-FSL: A Decentralized Federated Split Learning Solution for IoT on Hyperledger Fabric” 

### Architecture Overview

The system consists of four main components:
1.  **Hyperledger Fabric Network**: Provides the decentralized trust layer. It manages participant identities (via MSPs), records hashes of model updates, and enforces access control policies for private data collections.
2.  **IPFS (InterPlanetary File System)**: Acts as the off-chain storage for large data objects. Instead of bloating the blockchain, we store bulky ML data (activations, gradients, model weights) in IPFS and record only their immutable content identifiers (CIDs) on the ledger.
3.  **TypeScript API Servers**: These act as secure proxies between the Python ML application and the Fabric network. Each participating organization runs its own API server, which holds the cryptographic identity necessary to interact with the blockchain on its behalf.
4.  **Python ML Application**: This is the core Split Federated Learning application. It uses `PyTorch` to define and train the split models. It runs multiple client threads and a server thread, which communicate with their respective API servers to interact with the Fabric network and use a local IPFS interface for data exchange.

### Project Overview

This project implements Federated Split Learning on Hyperledger Fabric. Key components:

- **`.gitignore`**  
  Excludes build artifacts, credentials, and other ephemeral files.

- **`LICENSE`**  
  Project licensing terms.

- **`README.md`**  
  This overview and usage instructions.

- **`requirements.txt`**  
  Python dependencies for the Fabric-client and IPFS integration.

- **`api-ts/`**  
  TypeScript-based REST/WebSocket API bridge between the Python client and Fabric network.  
  - `src/client.ts` Fabric Gateway client  
  - `src/gateway.ts` Gateway helper  
  - `src/server.ts` Express/WebSocket server  
  - `package.json`, `tsconfig.json` TypeScript project config  
  - `startserver.sh` Build & launch script

- **`fsl-chaincode/`**  
  Chaincode definition and deployment artifacts.  
  - `chaincode/fsl_chaincode.go` Go smart contract implementing FSL logic  
  - `chaincode/collections_config.json` PDC policy file  
  - `deployFSL.sh` Installs and instantiates chaincode  
  - `generate_collections_config.py` Generates `collections_config.json` for arbitrary MSP lists

- **`src/fabric_project/`**  
  Python-based orchestrator and experiment runner.  
  - `client.py`, `server.py` Client/server roles for split learning  
  - `main.py` Entry point to launch experiments  
  - `models.py`, `evaluation.py` ML model definitions and metrics  
  - `ipfs_interface.py`, `storage.py` IPFS integration and results persistence  
  - `config.py` Experiment parameters (clients, epochs, network addresses)  
  - `startIpfs.sh` Local IPFS daemon launcher  
  - `results_fabric/` Output directory for experiment data

Use this overview to navigate the codebase and locate components for network setup, chaincode deployment, API services, or experiment execution.```

## Prerequisites

- Docker & Docker Compose  
- Hyperledger Fabric binaries (`peer`, `orderer`, `configtxgen`, `cryptogen`) in your `$PATH`  
- Go (v1.18+)  
- Node.js (v16+) & npm  
- Python 3.8+ & pip  
- IPFS CLI (`ipfs`)

### Step-by-Step Execution Guide
## Setup

1. **Clone the repository**  
   Use Git to clone this project and enter its directory.

2. **Install dependencies**  
   - In the `api-ts` folder, install Node.js packages with `npm install`.  
   - At the project root, install Python dependencies with `pip install -r requirements.txt`.

## Step 1: Launch Fabric Network

Navigate to your local Fabric test-network (from fabric-samples), bring up the network with Certificate Authorities and create the channel. This generates MSPs, CAs, peers, an orderer and joins default peers.

Return to the project root when complete.

## Step 2: Deploy Chaincode

In the `fsl-chaincode` directory, run the deployment script to package, install and instantiate the `fsl` smart contract on the channel.

## Step 3: Generate Collections Config for 3 Orgs

Use the Python script in `fsl-chaincode` to generate a Private Data Collections configuration for three organizations (Org1MSP, Org2MSP, Org3MSP), including the Admin MSP in the global policy. Save the output to `chaincode/collections_config.json`. You may need to re-run the deployment script after updating this file.

## Step 4: Start Services

- **IPFS**: In `src/fabric_project`, launch the local IPFS daemon for off-chain storage. Keep this terminal open.  
- **TypeScript APIs**: In `api-ts`, start the API servers that bridge the Python application and Fabric network. Before launching, update the `CLIENTS` array in `startserver.sh` to reflect the three organizations.

## Step 5: Run the SFL Experiment

In `src/fabric_project`, run the main Python application to kick off the federated split learning process. The experiment will use the number of clients and epochs specified in `config.py`, and will output results into the `results_fabric/` directory.

## Scaling to More Organizations

To extend beyond three organizations:

1. **Fabric Crypto**  
   Add a new OrgN entry to your crypto-config (or Fabric-CA registrar scripts) and generate MSP materials for OrgN.

2. **Channel Configuration**  
   Append `OrgNMSP` in `configtx.yaml` under the `Organizations` list and in your channel profile’s `Application.Organizations` section.

3. **Docker Compose**  
   Extend the test-network compose files to include a new peer service for `peer0.orgN.example.com`, ensuring unique port mappings.

4. **Network Scripts**  
   Update `envVar.sh` and `createChannel.sh` to include OrgN in the global variables and channel-join logic.

5. **Recreate Network**  
   Tear down the existing network, then bring it back up with the updated definitions and join all peers.

6. **Application Updates**  
   - Re-generate your PDC configuration including the new MSP.  
   - Update the `CLIENTS` array in `api-ts/startserver.sh`.  
   - Adjust `NUM_CLIENTS` in `src/fabric_project/config.py`.  
   - Re-deploy the chaincode and re-run the services and experiment.

Refer to the source paper for detailed protocol design and rationale. All scripts can be further modified for fully dynamic client configurations.```