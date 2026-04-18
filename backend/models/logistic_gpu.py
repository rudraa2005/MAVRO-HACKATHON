import torch
import torch.nn as nn
import torch.optim as optim
import os
import joblib

class LogisticRiskModel(nn.Module):
    """
    GPU-accelerated Logistic Regression replacement using PyTorch.
    Architecture: Linear(input_dim, 16) -> ReLU -> Linear(16, 1) -> Sigmoid
    """
    def __init__(self, input_dim):
        super(LogisticRiskModel, self).__init__()
        self.model = nn.Sequential(
            nn.Linear(input_dim, 16),
            nn.ReLU(),
            nn.Linear(16, 1),
            nn.Sigmoid()
        )
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.to(self.device)
        self.input_dim = input_dim

    def forward(self, x):
        return self.model(x)

    def train_model(self, X_train, y_train, epochs=30, batch_size=256, lr=0.001):
        """
        Standard training loop using BCE Loss and Adam optimizer.
        """
        self.train()
        X_tensor = torch.tensor(X_train, dtype=torch.float32).to(self.device)
        y_tensor = torch.tensor(y_train, dtype=torch.float32).view(-1, 1).to(self.device)

        criterion = nn.BCELoss()
        optimizer = optim.Adam(self.parameters(), lr=lr)

        dataset = torch.utils.data.TensorDataset(X_tensor, y_tensor)
        dataloader = torch.utils.data.DataLoader(dataset, batch_size=batch_size, shuffle=True)

        print(f"Training on {self.device}...")
        for epoch in range(epochs):
            epoch_loss = 0.0
            for batch_X, batch_y in dataloader:
                optimizer.zero_grad()
                outputs = self(batch_X)
                loss = criterion(outputs, batch_y)
                loss.backward()
                optimizer.step()
                epoch_loss += loss.item()
            
            if (epoch + 1) % 5 == 0 or epoch == 0:
                print(f"Epoch [{epoch+1}/{epochs}], Loss: {epoch_loss/len(dataloader):.4f}")

    def predict_proba(self, X):
        """
        Returns probabilities for class 1.
        """
        self.eval()
        with torch.no_grad():
            X_tensor = torch.tensor(X, dtype=torch.float32).to(self.device)
            probs = self(X_tensor).cpu().numpy()
        return probs.flatten()

    def save(self, path):
        """
        Saves model weights and input_dim.
        """
        torch.save({
            'model_state_dict': self.state_dict(),
            'input_dim': self.input_dim
        }, path)
        print(f"Model saved to {path}")

    @staticmethod
    def load(path):
        """
        Loads a saved model from path.
        """
        checkpoint = torch.load(path, map_location=torch.device("cuda" if torch.cuda.is_available() else "cpu"))
        model = LogisticRiskModel(checkpoint['input_dim'])
        model.load_state_dict(checkpoint['model_state_dict'])
        model.eval()
        return model

def train_gpu_logistic(X_train, y_train, input_dim, path="backend/models/final_gpu_model.pt"):
    """
    Utility function to initialize and train the model.
    """
    model = LogisticRiskModel(input_dim)
    model.train_model(X_train, y_train)
    model.save(path)
    return model
