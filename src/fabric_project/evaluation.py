import torch
from torch.utils.data import DataLoader
from typing import Tuple

from models import ClientModel, ServerModel


def evaluate(
    device: torch.device,
    client_model: ClientModel,
    server_model: ServerModel,
    criterion,
    test_loader: DataLoader,
) -> Tuple[float, float]:
    client_model.eval()
    server_model.eval()
    total_loss = 0.0
    correct = 0
    total = 0
    with torch.no_grad():
        for images, labels in test_loader:
            images, labels = images.to(device), labels.to(device)
            client_outputs = client_model(images)
            outputs = server_model(client_outputs)
            loss = criterion(outputs, labels)
            total_loss += loss.item() * labels.size(0)
            _, predicted = torch.max(outputs.data, 1)
            total += labels.size(0)
            correct += (predicted == labels).sum().item()
    test_loss = total_loss / total
    accuracy = 100 * correct / total
    return accuracy, test_loss
