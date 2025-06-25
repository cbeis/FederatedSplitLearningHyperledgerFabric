import torch.nn as nn
from torchvision import models

class ClientModel(nn.Module):
    def __init__(self):
        super(ClientModel, self).__init__()
        resnet = models.resnet18(weights=None)
        self.features = nn.Sequential(*list(resnet.children())[:5])

    def forward(self, x):
        return self.features(x)

class ServerModel(nn.Module):
    def __init__(self, num_classes: int = 10):
        super(ServerModel, self).__init__()
        resnet = models.resnet18(weights=None)
        self.rest_of_network = nn.Sequential(
            *list(resnet.children())[5:-1],
            nn.AdaptiveAvgPool2d((1, 1)),
            nn.Flatten(),
        )
        self.fc = nn.Linear(resnet.fc.in_features, num_classes)

    def forward(self, x):
        x = self.rest_of_network(x)
        return self.fc(x)
