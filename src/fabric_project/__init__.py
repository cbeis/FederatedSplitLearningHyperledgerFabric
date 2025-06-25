from .client import Client
from .server import Server
from .models import ClientModel, ServerModel
from .aggregation import perform_aggregation, local_update_barrier
from .evaluation import evaluate
from .config import NUM_CLIENTS, EPOCHS, BATCH_SIZE
