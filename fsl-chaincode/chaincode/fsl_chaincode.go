package main

import (
	"encoding/base64"
	"encoding/json"
	"fmt"

	"github.com/hyperledger/fabric-chaincode-go/pkg/cid"
	"github.com/hyperledger/fabric-contract-api-go/contractapi"
)

type SplitLearningContract struct {
	contractapi.Contract
}

type Data struct {
	IntermediateData string `json:"intermediateData,omitempty"`
	GradientData     string `json:"gradientData,omitempty"`
}

type ClientModelUpdate struct {
	RoundID        string `json:"roundID"`
	ModelParamHash string `json:"modelParamHash"`
	ClientID       string `json:"clientID"`
	DatasetSize    string `json:"datasetSize"`
}

type GlobalModelCommit struct {
	RoundID                   string `json:"roundID"`
	AggregatedGlobalModelHash string `json:"aggregatedGlobalModelHash"`
	IPFSCid                   string `json:"ipfsCid"`
	ClientID                  string `json:"clientID"`
}

func (s *SplitLearningContract) RegisterServer(ctx contractapi.TransactionContextInterface, topic string) (string, error) {
	creator, err := ctx.GetStub().GetCreator()
	if err != nil {
		return "", fmt.Errorf("failed to get creator: %v", err)
	}

	creatorBase64 := base64.StdEncoding.EncodeToString(creator)
	serverKey := fmt.Sprintf("server:%s", creatorBase64)

	// Check if the server is already registered
	existingTopic, err := ctx.GetStub().GetState(serverKey)
	if err != nil {
		return "", fmt.Errorf("failed to get state: %v", err)
	}
	if existingTopic != nil {
		return "", fmt.Errorf("server is already registered")
	}

	err = ctx.GetStub().PutState(serverKey, []byte(topic))
	if err != nil {
		return "", fmt.Errorf("failed to put state: %v", err)
	}
	return creatorBase64, nil // Return the server ID
}

func (s *SplitLearningContract) GetServerAddress(ctx contractapi.TransactionContextInterface) ([]string, error) {
	prefix := "server:A"
	sufix := "server:Z"

	resultsIterator, err := ctx.GetStub().GetStateByRange(prefix, sufix)
	if err != nil {
		return nil, fmt.Errorf("failed to get state: %v", err)
	}
	defer resultsIterator.Close()

	// Create a slice to store the server addresses
	var serverAddresses []string

	// Iterate through the result set and collect all server addresses
	for resultsIterator.HasNext() {
		queryResponse, err := resultsIterator.Next()
		if err != nil {
			return nil, fmt.Errorf("failed to iterate results: %v", err)
		}

		serverAddress := queryResponse.Key
		serverAddresses = append(serverAddresses, serverAddress)
	}

	if len(serverAddresses) == 0 {
		return nil, fmt.Errorf("no server addresses found")
	}

	return serverAddresses, nil
}

func (s *SplitLearningContract) RegisterClient(ctx contractapi.TransactionContextInterface, serverAddress string) (string, error) {
	creator, err := ctx.GetStub().GetCreator()
	if err != nil {
		return "", fmt.Errorf("failed to get creator: %v", err)
	}

	creatorBase64 := base64.StdEncoding.EncodeToString(creator)
	clientKey := fmt.Sprintf("clientToServer:%s", creatorBase64)

	existingServer, err := ctx.GetStub().GetState(clientKey)
	if err != nil {
		return "", fmt.Errorf("failed to get state: %v", err)
	}
	if existingServer != nil {
		return creatorBase64, nil
	}

	serverKey := fmt.Sprintf("server:%s", serverAddress)
	registeredServer, err := ctx.GetStub().GetState(serverKey)
	if err != nil {
		return "", fmt.Errorf("failed to get state: %v", err)
	}
	if registeredServer == nil {
		return "", fmt.Errorf("server is not registered")
	}

	err = ctx.GetStub().PutState(clientKey, []byte(serverAddress))
	if err != nil {
		return "", fmt.Errorf("failed to put state: %v", err)
	}
	return creatorBase64, nil // Return the client ID
}

// AddIntermediateData reads the IPFS CID from the transient map,
// stores it in the PDC, then records the private‐data hash on‐ledger
// and emits an event with that hash + TxID.
func (s *SplitLearningContract) AddIntermediateData(ctx contractapi.TransactionContextInterface) error {
	stub := ctx.GetStub()

	// ———————————————
	// 1. Who is calling? get both their serialized identity & their MSP ID
	// ———————————————
	creatorBytes, err := stub.GetCreator()
	if err != nil {
		return fmt.Errorf("GetCreator failed: %v", err)
	}
	clientID := base64.StdEncoding.EncodeToString(creatorBytes)

	mspID, err := cid.GetMSPID(stub)
	if err != nil {
		return fmt.Errorf("GetMSPID failed: %v", err)
	}

	// ———————————————
	// 2. Verify client→server binding
	// ———————————————
	assocKey := fmt.Sprintf("clientToServer:%s", clientID)
	serverAddr, err := stub.GetState(assocKey)
	if err != nil {
		return fmt.Errorf("get binding failed: %v", err)
	}
	if serverAddr == nil {
		return fmt.Errorf("client %s not bound to any server", clientID)
	}

	// ———————————————
	// 3. Pull the IPFS CID from transient map
	// ———————————————
	transientMap, err := stub.GetTransient()
	if err != nil {
		return fmt.Errorf("GetTransient failed: %v", err)
	}
	cidBytes, ok := transientMap["cid"]
	if !ok {
		return fmt.Errorf("transient 'cid' not found")
	}
	ipfsCID := string(cidBytes)
	// ———————————————
	// 4. Write into the per-MSP PDC
	// ———————————————
	mspMapKey := fmt.Sprintf("clientToMSP-%s", clientID)
	if err := stub.PutState(mspMapKey, []byte(mspID)); err != nil {
		return fmt.Errorf("failed to record client MSP: %v", err)
	}
	collName := fmt.Sprintf("intermediateDataHashCollection%s", mspID)
	pdcKey := fmt.Sprintf("intermediateData-%s", clientID)
	dataStruct := Data{IntermediateData: string(cidBytes)}
	dataBytes, err := json.Marshal(dataStruct)
	if err != nil {
		return fmt.Errorf("marshal failed: %v", err)
	}
	if err := stub.PutPrivateData(collName, pdcKey, dataBytes); err != nil {
		return fmt.Errorf("PutPrivateData failed on %s: %v", collName, err)
	}

	// ———————————————
	// 5. Grab the hash and write it on‐ledger
	// ———————————————
	hashBytes, err := stub.GetPrivateDataHash(collName, pdcKey)
	if err != nil {
		return fmt.Errorf("GetPrivateDataHash failed: %v", err)
	}
	ledgerKey := fmt.Sprintf("intermediateHash-%s", clientID)
	if err := stub.PutState(ledgerKey, hashBytes); err != nil {
		return fmt.Errorf("PutState failed: %v", err)
	}

	// 6. Emit an event (including MSP so server can pick it up)
	eventName := fmt.Sprintf("IntermediateDataAdded:%s", clientID)
	eventPayload, _ := json.Marshal(map[string]string{
		"dataHash": ipfsCID,
		"txID":     stub.GetTxID(),
		"mspID":    mspID,
	})
	if err := stub.SetEvent(eventName, eventPayload); err != nil {
		return fmt.Errorf("SetEvent failed: %v", err)
	}

	return nil
}

func (s *SplitLearningContract) AddGradients(ctx contractapi.TransactionContextInterface, clientBase64 string) error {

	stub := ctx.GetStub()

	// 1. Verify this server is indeed bound to that client
	creatorBytes, err := stub.GetCreator()
	if err != nil {
		return fmt.Errorf("GetCreator failed: %v", err)
	}
	serverID := base64.StdEncoding.EncodeToString(creatorBytes)

	bindKey := fmt.Sprintf("clientToServer:%s", clientBase64)
	binding, err := stub.GetState(bindKey)
	if err != nil {
		return fmt.Errorf("get binding failed: %v", err)
	}
	if binding == nil || string(binding) != serverID {
		return fmt.Errorf("client %s not bound to server %s", clientBase64, serverID)
	}

	// 2. Pull the CID from transient
	mspMapKey := fmt.Sprintf("clientToMSP-%s", clientBase64)
	mspBytes, err := stub.GetState(mspMapKey)
	if err != nil {
		return fmt.Errorf("failed to lookup client MSP: %v", err)
	}
	if len(mspBytes) == 0 {
		return fmt.Errorf("no MSP recorded for client %s", clientBase64)
	}
	clientMSP := string(mspBytes)

	transientMap, err := stub.GetTransient()
	if err != nil {
		return fmt.Errorf("GetTransient failed: %v", err)
	}
	gradCID, ok := transientMap["cid"]
	if !ok {
		return fmt.Errorf("transient 'cid' not found")
	}
	ipfsCID := string(gradCID)
	// 3. Write to PDC
	collName := fmt.Sprintf("intermediateDataHashCollection%s", clientMSP)
	pdcKey := fmt.Sprintf("gradients-%s", clientBase64)
	dataStruct := Data{GradientData: string(gradCID)}
	dataBytes, _ := json.Marshal(dataStruct)
	if err := stub.PutPrivateData(collName, pdcKey, dataBytes); err != nil {
		return fmt.Errorf("PutPrivateData failed on %s: %v", collName, err)
	}

	// 4. Record hash on-ledger
	hashBytes, err := stub.GetPrivateDataHash(collName, pdcKey)
	if err != nil {
		return fmt.Errorf("GetPrivateDataHash failed: %v", err)
	}
	ledgerKey := fmt.Sprintf("gradientsHash-%s", clientBase64)
	if err := stub.PutState(ledgerKey, hashBytes); err != nil {
		return fmt.Errorf("PutState failed: %v", err)
	}

	// 6. Emit event (include MSP again for clarity)
	eventName := fmt.Sprintf("GradientsAdded:%s", clientBase64)
	eventPayload, _ := json.Marshal(map[string]string{
		"dataHash": ipfsCID,
		"txID":     stub.GetTxID(),
		"mspID":    clientMSP,
	})
	if err := stub.SetEvent(eventName, eventPayload); err != nil {
		return fmt.Errorf("SetEvent failed: %v", err)
	}

	return nil
}

func (s *SplitLearningContract) SubmitClientModelHash(ctx contractapi.TransactionContextInterface, roundID string, datasetSizeStr string) error {
	stub := ctx.GetStub()

	// ——————————
	// 1. Who is calling? identity + MSP
	// ——————————
	creator, err := stub.GetCreator()
	if err != nil {
		return fmt.Errorf("GetCreator failed: %v", err)
	}
	clientID := base64.StdEncoding.EncodeToString(creator)

	mspID, err := cid.GetMSPID(stub)
	if err != nil {
		return fmt.Errorf("GetMSPID failed: %v", err)
	}

	// ——————————
	// 2. Pull the model-hash from transient
	// ——————————
	tm, err := stub.GetTransient()
	if err != nil {
		return fmt.Errorf("GetTransient failed: %v", err)
	}
	hashBytes, ok := tm["modelHash"]
	if !ok {
		return fmt.Errorf("transient field 'modelHash' not found")
	}
	modelHash := string(hashBytes)

	// ——————————
	// 4. Write full update JSON into the per-MSP PDC
	// ——————————
	collName := fmt.Sprintf("clientModelHashCollection%s", mspID)
	update := ClientModelUpdate{
		RoundID:        roundID,
		ModelParamHash: modelHash,
		ClientID:       clientID,
		DatasetSize:    datasetSizeStr,
	}
	updBytes, err := json.Marshal(update)
	if err != nil {
		return fmt.Errorf("marshal update failed: %v", err)
	}
	pdcKey := fmt.Sprintf("clientUpdate-%s-%s", roundID, clientID)
	if err := stub.PutPrivateData(collName, pdcKey, updBytes); err != nil {
		return fmt.Errorf("PutPrivateData failed on %s: %v", collName, err)
	}

	// ——————————
	// 5. Grab Fabric’s SHA-256 of that private entry, write it on-ledger
	// ——————————
	hashOnLedger, err := stub.GetPrivateDataHash(collName, pdcKey)
	if err != nil {
		return fmt.Errorf("GetPrivateDataHash failed: %v", err)
	}
	refKey := fmt.Sprintf("clientModelHashRef-%s-%s", roundID, clientID)
	if err := stub.PutState(refKey, hashOnLedger); err != nil {
		return fmt.Errorf("PutState failed: %v", err)
	}

	return nil
}

// triggerClientAggregation initiates the aggregation process for a given round.
// It reads all model hashes from the PDC and emits an event for clients to start aggregation.
func (s *SplitLearningContract) TriggerClientAggregation(ctx contractapi.TransactionContextInterface, roundID string) error {
	stub := ctx.GetStub()

	// 1) List the MSPs whose PDCs we need to scan
	//    (adjust this slice to match your network)
	msps := []string{"Org1MSP", "Org2MSP"}

	prefix := fmt.Sprintf("clientUpdate-%s-", roundID)
	var allUpdates []ClientModelUpdate
	endKey := prefix + "\u00FF"

	// 2) For each MSP’s PDC, pull every key and fetch its private-data hash
	for _, mspID := range msps {
		collName := fmt.Sprintf("clientModelHashCollection%s", mspID)
		iter, err := stub.GetPrivateDataByRange(collName, prefix, endKey)
		if err != nil {
			return fmt.Errorf("GetPrivateDataByRange failed on %s [%s…%s]: %v",
				collName, prefix, endKey, err)
		}
		defer iter.Close()

		for iter.HasNext() {
			qr, err := iter.Next()
			if err != nil {
				return fmt.Errorf("PDC iterator error on %s: %v", collName, err)
			}

			// 3) Read the actual private‐data bytes
			dataBytes, err := stub.GetPrivateData(collName, qr.Key)
			if err != nil {
				return fmt.Errorf("GetPrivateData failed for %s/%s: %v",
					collName, qr.Key, err)
			}
			if len(dataBytes) == 0 {
				return fmt.Errorf("empty private data for %s/%s", collName, qr.Key)
			}

			// 4) Unmarshal into your struct
			var upd ClientModelUpdate
			if err := json.Unmarshal(dataBytes, &upd); err != nil {
				return fmt.Errorf("unmarshal update failed for %s/%s: %v",
					collName, qr.Key, err)
			}

			allUpdates = append(allUpdates, upd)

		}
	}

	if len(allUpdates) == 0 {
		return fmt.Errorf("no client updates found for round %s", roundID)
	}

	eventName := fmt.Sprintf("AggregationTaskStart:%s", roundID)
	payload := struct {
		RoundID string              `json:"roundID"`
		Updates []ClientModelUpdate `json:"updates"`
	}{
		RoundID: roundID,
		Updates: allUpdates,
	}

	payloadBytes, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("failed to marshal event payload: %v", err)
	}
	if err := stub.SetEvent(eventName, payloadBytes); err != nil {
		return fmt.Errorf("failed to emit %s: %v", eventName, err)
	}

	return nil
}

// commitGlobalModelHash is called by each client after they compute the aggregated model.
// It stores their proposed global hash in 'globalModelHashCollection' for consensus checking.
func (s *SplitLearningContract) CommitGlobalModelHash(ctx contractapi.TransactionContextInterface, roundID string, aggregatedGlobalModelHash string, ipfsCid string) error {
	creator, err := ctx.GetStub().GetCreator()
	if err != nil {
		return fmt.Errorf("failed to get creator: %v", err)
	}
	clientID := base64.StdEncoding.EncodeToString(creator)

	commit := GlobalModelCommit{
		RoundID:                   roundID,
		AggregatedGlobalModelHash: aggregatedGlobalModelHash,
		IPFSCid:                   ipfsCid,
		ClientID:                  clientID,
	}
	commitBytes, err := json.Marshal(commit)
	if err != nil {
		return fmt.Errorf("failed to marshal global model commit: %v", err)
	}

	pdcKey := fmt.Sprintf("globalCommit-%s-%s", roundID, clientID)
	err = ctx.GetStub().PutPrivateData("globalModelHashCollection", pdcKey, commitBytes)
	if err != nil {
		return fmt.Errorf("failed to put global model commit in PDC: %v", err)
	}

	return nil
}

// endGlobalModel checks for consensus on the committed global model hashes.
// If consensus is met, it saves the final hash to the world state and emits a final event.
func (s *SplitLearningContract) EndGlobalModel(ctx contractapi.TransactionContextInterface, roundID string) error {
	// This function must have a strict endorsement policy (e.g., majority of clients must endorse)
	iter, err := ctx.GetStub().GetPrivateDataByRange(
		"globalModelHashCollection",
		"globalCommit-"+roundID+"-",
		"globalCommit-"+roundID+"-\u00FF",
	)
	if err != nil {
		return err
	}
	defer iter.Close()

	// tally votes by PureHash
	voteCounts := map[string]int{}
	// remember an example IPFSCid for each hash
	cidForHash := map[string]string{}
	total := 0

	for iter.HasNext() {
		qr, _ := iter.Next()
		var c GlobalModelCommit
		json.Unmarshal(qr.Value, &c)
		voteCounts[c.AggregatedGlobalModelHash]++
		total++
		if _, seen := cidForHash[c.AggregatedGlobalModelHash]; !seen {
			cidForHash[c.AggregatedGlobalModelHash] = c.IPFSCid
		}
	}

	var winnerHash string
	maxVotes := 0
	for h, cnt := range voteCounts {
		if cnt > maxVotes {
			maxVotes, winnerHash = cnt, h
		}
	}
	if maxVotes == 0 {
		return fmt.Errorf("no commits for round %s", roundID)
	}

	// 2) Enforce threshold (e.g. 2/3)
	const quorumRatio = 0.66
	if float64(maxVotes)/float64(total) < quorumRatio {
		return fmt.Errorf(
			"consensus not met for round %s: %d/%d votes (need ≥ %.0f%%)",
			roundID,
			maxVotes,
			total,
			quorumRatio*100,
		)
	}

	// emit event carrying *the IPFS CID* from the winning group
	finalCid := cidForHash[winnerHash]
	eventName := fmt.Sprintf("GlobalModelUpdated:%s", roundID)
	return ctx.GetStub().SetEvent(eventName, []byte(finalCid))
}

func main() {
	chaincode, err := contractapi.NewChaincode(&SplitLearningContract{})
	if err != nil {
		return
	}

	if err := chaincode.Start(); err != nil {
	}
}
