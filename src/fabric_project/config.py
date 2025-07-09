import threading


NUM_CLIENTS = 2
EPOCHS = 2
BATCH_SIZE = 128
local_update_barrier = threading.Barrier(NUM_CLIENTS+1)
global_commit_barrier = threading.Barrier(NUM_CLIENTS+1)