#!/usr/bin/env python

import os
import logging
import argparse
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from tqdm import tqdm
from torch.utils.data import Dataset, DataLoader
from sklearn.model_selection import train_test_split
from sklearn.metrics import (
    accuracy_score,
    f1_score,
    roc_auc_score,
    average_precision_score,
    precision_recall_fscore_support
)

try:
    from torchdiffeq import odeint
    TORCHDIFFEQ_AVAILABLE = True
except ImportError:
    TORCHDIFFEQ_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.FileHandler("training_lr3.log", mode='w'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def parse_args():
    parser = argparse.ArgumentParser(description="Train multiple longitudinal models for CKD classification, including RNN, LSTM, Transformer, MLP, TCN, and Neural ODE, with label-switch analysis.")
    parser.add_argument("--embedding-root", type=str, default="./ckd_embeddings_100", help="Path to embeddings.")
    parser.add_argument("--window-size", type=int, default=10, help="Sequence window size.")
    parser.add_argument("--embed-dim", type=int, default=768, help="Dimensionality of embeddings.")
    parser.add_argument("--epochs", type=int, default=50, help="Number of epochs per model.")
    parser.add_argument("--batch-size", type=int, default=64, help="Batch size.")
    parser.add_argument("--lr", type=float, default=5e-3, help="Learning rate.")
    parser.add_argument("--patience", type=int, default=5, help="Early stopping patience.")
    parser.add_argument("--scheduler-patience", type=int, default=2, help="Patience for scheduler LR reduction.")
    parser.add_argument("--metadata-file", type=str, default="patient_embedding_metadata.csv", help="CSV with metadata.")
    parser.add_argument("--random-seed", type=int, default=42, help="Random seed.")
    parser.add_argument("--hidden-dim", type=int, default=128, help="Hidden dimension for RNN/LSTM/ODE/TCN.")
    parser.add_argument("--num-layers", type=int, default=2, help="Layers for RNN/LSTM/Transformer/TCN/ODE MLP block.")
    parser.add_argument("--rnn-dropout", type=float, default=0.2, help="Dropout in RNN/LSTM.")
    parser.add_argument("--rnn-bidir", action="store_true", help="Use bidirectional RNN/LSTM if set.")
    parser.add_argument("--transformer-nhead", type=int, default=4, help="Heads in Transformer encoder.")
    parser.add_argument("--transformer-dim-feedforward", type=int, default=256, help="Transformer feedforward dim.")
    parser.add_argument("--transformer-dropout", type=float, default=0.2, help="Dropout in Transformer layers.")
    parser.add_argument("--max-patients", type=int, default=None, help="If set, only load embeddings for up to this many patients (for debugging).")
    parser.add_argument("--output-model-prefix", type=str, default="best_model", help="Filename prefix for saved models.")
    return parser.parse_args()

def clean_ckd_stage(value):
    try:
        return int(value)
    except ValueError:
        if isinstance(value, str) and value[0].isdigit():
            return int(value[0])
        else:
            return np.nan

def embedding_exists(row, root):
    return os.path.exists(os.path.join(root, row["embedding_file"]))

def load_embedding(full_path, cache):
    if full_path not in cache:
        with np.load(full_path) as data:
            keys = list(data.keys())
            cache[full_path] = data[keys[0]]
    return cache[full_path]

def monotonic_labels(labels):
    monotonic = []
    has_progressed = False
    for lab in labels:
        if lab == 1:
            has_progressed = True
        monotonic.append(1 if has_progressed else 0)
    return monotonic

def build_sequences(meta, window_size):
    # Returns a list of (context_embeddings, label, patient_id, local_index_in_seq).
    sequence_data = []
    for pid, group in meta.groupby("PatientID"):
        group = group.sort_values(by="EventDate")
        embeddings = list(group["embedding"])
        original_labels = list(group["next_label"])
        labels = monotonic_labels(original_labels)
        for i in range(1, len(embeddings)):
            start_idx = max(0, i - window_size)
            context = embeddings[start_idx:i]
            target = labels[i - 1]
            sequence_data.append((context, target, pid, i - 1))
    return sequence_data

def pad_sequence(seq, length, dim):
    if len(seq) < length:
        padding = [np.zeros(dim)] * (length - len(seq))
        seq = padding + seq
    return np.stack(seq[-length:], axis=0)

class CKDSequenceDataset(Dataset):
    def __init__(self, data, window_size, embed_dim):
        self.data = data
        self.window_size = window_size
        self.embed_dim = embed_dim

    def __len__(self):
        return len(self.data)

    def __getitem__(self, idx):
        context, label, pid, local_idx = self.data[idx]
        context_padded = pad_sequence(context, self.window_size, self.embed_dim)
        return (
            torch.tensor(context_padded, dtype=torch.float32),
            torch.tensor(label, dtype=torch.long),
            pid,
            local_idx
        )

class LongitudinalRNN(nn.Module):
    def __init__(self, input_dim, hidden_dim, num_layers, dropout=0.1, bidirectional=False):
        super().__init__()
        self.bidirectional = bidirectional
        self.num_layers = num_layers
        self.hidden_dim = hidden_dim
        self.num_directions = 2 if bidirectional else 1
        self.rnn = nn.GRU(
            input_dim,
            hidden_dim,
            num_layers=num_layers,
            batch_first=True,
            dropout=(dropout if num_layers > 1 else 0.0),
            bidirectional=bidirectional
        )
        self.classifier = nn.Linear(hidden_dim * self.num_directions, 2)

    def forward(self, x):
        _, h_n = self.rnn(x)
        h_n = h_n.view(self.num_layers, self.num_directions, x.size(0), self.hidden_dim)
        top_layer = h_n[-1]
        top_layer = top_layer.transpose(0, 1).contiguous().view(x.size(0), -1)
        return self.classifier(top_layer)

class LongitudinalLSTM(nn.Module):
    def __init__(self, input_dim, hidden_dim, num_layers, dropout=0.1, bidirectional=False):
        super().__init__()
        self.bidirectional = bidirectional
        self.num_layers = num_layers
        self.hidden_dim = hidden_dim
        self.num_directions = 2 if bidirectional else 1
        self.lstm = nn.LSTM(
            input_dim,
            hidden_dim,
            num_layers=num_layers,
            batch_first=True,
            dropout=(dropout if num_layers > 1 else 0.0),
            bidirectional=bidirectional
        )
        self.classifier = nn.Linear(hidden_dim * self.num_directions, 2)

    def forward(self, x):
        _, (h_n, _) = self.lstm(x)
        h_n = h_n.view(self.num_layers, self.num_directions, x.size(0), self.hidden_dim)
        top_layer = h_n[-1]
        top_layer = top_layer.transpose(0, 1).contiguous().view(x.size(0), -1)
        return self.classifier(top_layer)

class PositionalEncoding(nn.Module):
    def __init__(self, d_model, max_len=5000):
        super().__init__()
        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2).float() * -(np.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        self.pe = pe.unsqueeze(0)

    def forward(self, x):
        seq_len = x.size(1)
        return x + self.pe[:, :seq_len, :].to(x.device)

class LongitudinalTransformer(nn.Module):
    def __init__(self, input_dim, num_layers, nhead, dim_feedforward, dropout=0.1):
        super().__init__()
        self.pos_encoder = PositionalEncoding(input_dim)
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=input_dim,
            nhead=nhead,
            dim_feedforward=dim_feedforward,
            dropout=dropout,
            batch_first=True
        )
        self.transformer_encoder = nn.TransformerEncoder(encoder_layer, num_layers=num_layers)
        self.classifier = nn.Linear(input_dim, 2)

    def forward(self, x):
        out = self.pos_encoder(x)
        out = self.transformer_encoder(out)
        last_token = out[:, -1, :]
        return self.classifier(last_token)

class MLPSimple(nn.Module):
    def __init__(self, input_dim, window_size, hidden_dim, num_layers=2, dropout=0.1):
        super().__init__()
        self.flatten_dim = input_dim * window_size
        layers = []
        current_dim = self.flatten_dim
        for _ in range(num_layers - 1):
            layers.append(nn.Linear(current_dim, hidden_dim))
            layers.append(nn.ReLU())
            layers.append(nn.Dropout(dropout))
            current_dim = hidden_dim
        layers.append(nn.Linear(current_dim, 2))
        self.net = nn.Sequential(*layers)

    def forward(self, x):
        x = x.view(x.size(0), -1)
        return self.net(x)

class TemporalBlock(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, stride, dilation, padding, dropout):
        super().__init__()
        self.conv1 = nn.Conv1d(
            in_channels, out_channels, kernel_size,
            stride=stride, padding=padding, dilation=dilation
        )
        self.bn1 = nn.BatchNorm1d(out_channels)
        self.relu1 = nn.ReLU()
        self.dropout1 = nn.Dropout(dropout)

        self.conv2 = nn.Conv1d(
            out_channels, out_channels, kernel_size,
            stride=stride, padding=padding, dilation=dilation
        )
        self.bn2 = nn.BatchNorm1d(out_channels)
        self.relu2 = nn.ReLU()
        self.dropout2 = nn.Dropout(dropout)

        self.downsample = nn.Conv1d(in_channels, out_channels, 1) if in_channels != out_channels else None

    def forward(self, x):
        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu1(out)
        out = self.dropout1(out)
        out = self.conv2(out)
        out = self.bn2(out)
        # Slice the output so it matches x's length for the residual connection
        out = out[:, :, :x.size(2)]
        if self.downsample is not None:
            x = self.downsample(x)
        out += x
        out = self.relu2(out)
        out = self.dropout2(out)
        return out

class TCN(nn.Module):
    def __init__(self, input_dim, hidden_dim, num_layers=2, kernel_size=3, dropout=0.1):
        super().__init__()
        layers = []
        in_channels = input_dim
        for i in range(num_layers):
            out_channels = hidden_dim
            dilation_size = 2**i
            padding = (kernel_size - 1) * dilation_size
            block = TemporalBlock(
                in_channels, out_channels, kernel_size=kernel_size,
                stride=1, dilation=dilation_size, padding=padding, dropout=dropout
            )
            layers.append(block)
            in_channels = out_channels
        self.network = nn.Sequential(*layers)
        self.classifier = nn.Linear(hidden_dim, 2)

    def forward(self, x):
        x = x.permute(0, 2, 1)
        out = self.network(x)
        last_time = out[:, :, -1]
        return self.classifier(last_time)

class ODEFunc(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(dim, dim),
            nn.ReLU(),
            nn.Linear(dim, dim)
        )

    def forward(self, t, x):
        return self.net(x)

class NeuralODEModel(nn.Module):
    def __init__(self, input_dim, hidden_dim):
        super().__init__()
        self.input_dim = input_dim
        self.hidden_dim = hidden_dim
        self.encoder = nn.Linear(input_dim, hidden_dim)
        self.odefunc = ODEFunc(hidden_dim)
        self.classifier = nn.Linear(hidden_dim, 2)

    def forward(self, x):
        if not TORCHDIFFEQ_AVAILABLE:
            raise ImportError("torchdiffeq is not installed. Please install it or remove the Neural ODE model.")
        batch_size = x.size(0)
        x_last = x[:, -1, :]
        z0 = self.encoder(x_last)
        t_span = torch.tensor([0, 1], dtype=torch.float).to(x.device)
        zt = odeint(self.odefunc, z0, t_span)
        z_final = zt[-1]
        return self.classifier(z_final)

def train_and_evaluate(model, device, train_loader, val_loader, test_loader, args, model_name):
    logger.info(f"Starting {model_name} training.")
    model.to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
    criterion = nn.CrossEntropyLoss()
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, factor=0.5, patience=args.scheduler_patience)
    best_val_loss = float('inf')
    epochs_no_improve = 0
    best_model_path = f"{args.output_model_prefix}_{model_name}.pt"

    for epoch in range(args.epochs):
        model.train()
        train_losses = []
        for batch in train_loader:
            x_batch, y_batch, _, _ = batch
            x_batch = x_batch.to(device)
            y_batch = y_batch.to(device)
            optimizer.zero_grad()
            logits = model(x_batch)
            loss = criterion(logits, y_batch)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), max_norm=5.0)
            optimizer.step()
            train_losses.append(loss.item())

        model.eval()
        val_losses = []
        with torch.no_grad():
            for batch in val_loader:
                x_batch, y_batch, _, _ = batch
                x_batch = x_batch.to(device)
                y_batch = y_batch.to(device)
                logits = model(x_batch)
                loss = criterion(logits, y_batch)
                val_losses.append(loss.item())

        avg_train_loss = np.mean(train_losses)
        avg_val_loss = np.mean(val_losses)
        logger.info(f"{model_name} Epoch {epoch+1}/{args.epochs} -> Train Loss: {avg_train_loss:.4f}, Val Loss: {avg_val_loss:.4f}")
        scheduler.step(avg_val_loss)

        if avg_val_loss < best_val_loss:
            best_val_loss = avg_val_loss
            epochs_no_improve = 0
            torch.save(model.state_dict(), best_model_path)
            logger.info(f"{model_name}: Validation loss improved. Model saved.")
        else:
            epochs_no_improve += 1
            if epochs_no_improve >= args.patience:
                logger.info(f"{model_name}: Early stopping triggered.")
                break

    logger.info(f"{model_name}: Loading best model for final evaluation.")
    model.load_state_dict(torch.load(best_model_path))
    model.eval()
    all_preds, all_probs, all_labels = [], [], []
    with torch.no_grad():
        for batch in test_loader:
            x_batch, y_batch, _, _ = batch
            x_batch = x_batch.to(device)
            logits = model(x_batch)
            probs = nn.Softmax(dim=1)(logits)
            preds = torch.argmax(probs, dim=1).cpu().numpy()
            all_preds.extend(preds)
            all_probs.extend(probs[:, 1].cpu().numpy())
            all_labels.extend(y_batch.numpy())

    accuracy = accuracy_score(all_labels, all_preds)
    f1 = f1_score(all_labels, all_preds)
    precision, recall, _, _ = precision_recall_fscore_support(all_labels, all_preds, average='binary')
    auroc = roc_auc_score(all_labels, all_probs)
    auprc = average_precision_score(all_labels, all_probs)
    logger.info(f"{model_name} Test Accuracy: {accuracy:.4f}")
    logger.info(f"{model_name} Test F1 Score: {f1:.4f}")
    logger.info(f"{model_name} Test Precision: {precision:.4f}")
    logger.info(f"{model_name} Test Recall: {recall:.4f}")
    logger.info(f"{model_name} Test AUROC: {auroc:.4f}")
    logger.info(f"{model_name} Test AUPRC: {auprc:.4f}")
    logger.info(f"{model_name} training complete.")
    return {
        "model_name": model_name,
        "accuracy": accuracy,
        "f1": f1,
        "precision": precision,
        "recall": recall,
        "auroc": auroc,
        "auprc": auprc
    }

def predict_label_switches(model, loader, device):
    model.eval()
    records = []
    with torch.no_grad():
        for batch in loader:
            x_batch, y_batch, pid_batch, idx_batch = batch
            x_batch = x_batch.to(device)
            logits = model(x_batch)
            probs = nn.Softmax(dim=1)(logits)
            preds = torch.argmax(probs, dim=1).cpu().numpy()
            for pid, true_label, pred_label, local_idx in zip(pid_batch, y_batch.numpy(), preds, idx_batch.numpy()):
                records.append((pid, local_idx, true_label, pred_label))
    df = pd.DataFrame(records, columns=["PatientID", "LocalIndex", "TrueLabel", "PredLabel"])
    return df

def analyze_switches(df):
    # Group by PatientID, find the first time the true label is 1, the first time the model predicted 1
    analysis = []
    for pid, group in df.groupby("PatientID"):
        group = group.sort_values("LocalIndex").reset_index(drop=True)
        true_switch = group[group["TrueLabel"] == 1]["LocalIndex"].min()
        pred_switch = group[group["PredLabel"] == 1]["LocalIndex"].min()
        analysis.append({
            "PatientID": pid,
            "TrueSwitchIdx": true_switch if pd.notnull(true_switch) else None,
            "PredSwitchIdx": pred_switch if pd.notnull(pred_switch) else None
        })
    analysis_df = pd.DataFrame(analysis)
    analysis_df["SwitchDifference"] = analysis_df["PredSwitchIdx"] - analysis_df["TrueSwitchIdx"]
    return analysis_df

def main():
    args = parse_args()

    # Log the configuration parameters at the start
    logger.info("Running with the following configuration:")
    for key, val in vars(args).items():
        logger.info(f"{key}: {val}")

    np.random.seed(args.random_seed)
    torch.manual_seed(args.random_seed)

    logger.info("Loading metadata.")
    metadata_path = os.path.join(args.embedding_root, args.metadata_file)
    metadata = pd.read_csv(metadata_path)
    metadata['CKD_stage_clean'] = metadata['CKD_stage'].apply(clean_ckd_stage)
    metadata = metadata.sort_values(by=['PatientID', 'EventDate'])
    metadata['CKD_stage_clean'] = metadata.groupby('PatientID')['CKD_stage_clean'].bfill()
    metadata = metadata.dropna(subset=['CKD_stage_clean'])
    metadata['CKD_stage_clean'] = metadata['CKD_stage_clean'].astype(int)
    metadata['label'] = metadata['CKD_stage_clean'].apply(lambda x: 0 if x < 4 else 1)
    metadata['next_label'] = metadata.groupby('PatientID')['label'].shift(-1)
    metadata = metadata.dropna(subset=['next_label'])
    metadata['next_label'] = metadata['next_label'].astype(int)

    logger.info("Filtering valid embedding rows.")
    metadata = metadata[metadata.apply(lambda row: embedding_exists(row, args.embedding_root), axis=1)]

    unique_patients = sorted(metadata['PatientID'].unique())
    if args.max_patients is not None and args.max_patients < len(unique_patients):
        subset_patients = unique_patients[: args.max_patients]
        metadata = metadata[metadata['PatientID'].isin(subset_patients)]
        logger.info(f"Using only {args.max_patients} patients for debugging.")

    embedding_cache = {}
    def load_embedding_for_row(row):
        path = os.path.join(args.embedding_root, row['embedding_file'])
        return load_embedding(path, embedding_cache)

    logger.info("Loading embeddings.")
    metadata['embedding'] = metadata.apply(load_embedding_for_row, axis=1)

    logger.info("Creating train/val/test splits.")
    unique_patients = metadata['PatientID'].unique()
    train_patients, test_patients = train_test_split(unique_patients, test_size=0.2, random_state=args.random_seed)
    train_patients, val_patients = train_test_split(train_patients, test_size=0.1, random_state=args.random_seed)

    train_metadata = metadata[metadata['PatientID'].isin(train_patients)]
    val_metadata = metadata[metadata['PatientID'].isin(val_patients)]
    test_metadata = metadata[metadata['PatientID'].isin(test_patients)]

    logger.info("Building sequence datasets with monotonic progression constraints.")
    train_sequences = build_sequences(train_metadata, args.window_size)
    val_sequences = build_sequences(val_metadata, args.window_size)
    test_sequences = build_sequences(test_metadata, args.window_size)

    train_dataset = CKDSequenceDataset(train_sequences, args.window_size, args.embed_dim)
    val_dataset = CKDSequenceDataset(val_sequences, args.window_size, args.embed_dim)
    test_dataset = CKDSequenceDataset(test_sequences, args.window_size, args.embed_dim)

    train_loader = DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True)
    val_loader = DataLoader(val_dataset, batch_size=args.batch_size, shuffle=False)
    test_loader = DataLoader(test_dataset, batch_size=args.batch_size, shuffle=False)

    device = torch.device("cuda:3" if torch.cuda.is_available() else "cpu")

    logger.info("Defining our models.")
    improved_rnn = LongitudinalRNN(
        input_dim=args.embed_dim,
        hidden_dim=args.hidden_dim,
        num_layers=args.num_layers,
        dropout=args.rnn_dropout,
        bidirectional=args.rnn_bidir
    )
    improved_lstm = LongitudinalLSTM(
        input_dim=args.embed_dim,
        hidden_dim=args.hidden_dim,
        num_layers=args.num_layers,
        dropout=args.rnn_dropout,
        bidirectional=args.rnn_bidir
    )
    improved_transformer = LongitudinalTransformer(
        input_dim=args.embed_dim,
        num_layers=args.num_layers,
        nhead=args.transformer_nhead,
        dim_feedforward=args.transformer_dim_feedforward,
        dropout=args.transformer_dropout
    )
    mlp_model = MLPSimple(
        input_dim=args.embed_dim,
        window_size=args.window_size,
        hidden_dim=args.hidden_dim,
        num_layers=args.num_layers,
        dropout=args.rnn_dropout
    )
    tcn_model = TCN(
        input_dim=args.embed_dim,
        hidden_dim=args.hidden_dim,
        num_layers=args.num_layers,
        kernel_size=3,
        dropout=args.rnn_dropout
    )
    ode_model = NeuralODEModel(
        input_dim=args.embed_dim,
        hidden_dim=args.hidden_dim
    )

    logger.info("Training the RNN, LSTM, Transformer, MLP, TCN, and Neural ODE models.")
    rnn_results = train_and_evaluate(improved_rnn, device, train_loader, val_loader, test_loader, args, model_name="ImprovedRNN")
    lstm_results = train_and_evaluate(improved_lstm, device, train_loader, val_loader, test_loader, args, model_name="ImprovedLSTM")
    transformer_results = train_and_evaluate(improved_transformer, device, train_loader, val_loader, test_loader, args, model_name="ImprovedTransformer")
    mlp_results = train_and_evaluate(mlp_model, device, train_loader, val_loader, test_loader, args, model_name="MLP")
    tcn_results = train_and_evaluate(tcn_model, device, train_loader, val_loader, test_loader, args, model_name="TCN")

    if TORCHDIFFEQ_AVAILABLE:
        ode_results = train_and_evaluate(ode_model, device, train_loader, val_loader, test_loader, args, model_name="NeuralODE")
    else:
        logger.info("torchdiffeq not installed, skipping Neural ODE training.")
        ode_results = None

    logger.info("All trainings complete. Summary of final test metrics follows.")
    all_results = [rnn_results, lstm_results, transformer_results, mlp_results, tcn_results, ode_results]
    for result in all_results:
        if result is None:
            continue
        logger.info(
            f"Model={result['model_name']} "
            f"Accuracy={result['accuracy']:.4f} "
            f"F1={result['f1']:.4f} "
            f"Precision={result['precision']:.4f} "
            f"Recall={result['recall']:.4f} "
            f"AUROC={result['auroc']:.4f} "
            f"AUPRC={result['auprc']:.4f}"
        )

    logger.info("Analyzing label switches for the trained models on the test set.")
    models = [
        ("ImprovedRNN", improved_rnn),
        ("ImprovedLSTM", improved_lstm),
        ("ImprovedTransformer", improved_transformer),
        ("MLP", mlp_model),
        ("TCN", tcn_model)
    ]
    if ode_results is not None:
        models.append(("NeuralODE", ode_model))

    for name, model_obj in models:
        df_preds = predict_label_switches(model_obj, test_loader, device)
        df_analysis = analyze_switches(df_preds)
        logger.info(f"Label-switch analysis for {name}:")
        logger.info(f"\n{df_analysis.head(10)}\n(Showing up to 10 rows)")

if __name__ == "__main__":
    main()
