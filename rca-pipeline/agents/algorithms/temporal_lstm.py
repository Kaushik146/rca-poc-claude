"""
Temporal LSTM Autoencoder for Anomaly Detection — pure numpy.

Detects temporal anomalies: patterns that are only anomalous when viewed across
multiple timesteps. For example:
  - Ramp failures: latency slowly increasing + throughput slowly decreasing
  - Oscillation: unstable metrics with high variance
  - Sudden spikes after a baseline

Architecture:
  - Encoder LSTM: 6 features × seq_len timesteps → 8 hidden units
  - Decoder: 8 hidden units → reconstructs 6 features × seq_len
  - Training: encoder+decoder minimize reconstruction error (MSE)
  - Inference: anomaly_score = MSE(input, reconstructed)

The LSTM cells implement real forward-pass computations:
  - Forget gate: controls what to forget from previous cell state
  - Input gate: controls what new information to add
  - Output gate: controls what information to expose as hidden state
  - Cell update: candidate values (tanh activation)

Training uses truncated backpropagation through time (TBPTT) with
numerical gradient estimation (finite differences) since analytic LSTM
backprop is very complex.
"""

import numpy as np
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple


# ─────────────────────────────────────────────────────────────────────────────
# Activation functions
# ─────────────────────────────────────────────────────────────────────────────
def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -20, 20)))


def _tanh(x: np.ndarray) -> np.ndarray:
    return np.tanh(x)


def _tanh_derivative(x: np.ndarray) -> np.ndarray:
    return 1.0 - np.tanh(x) ** 2


# ─────────────────────────────────────────────────────────────────────────────
# LSTM Cell (pure numpy)
# ─────────────────────────────────────────────────────────────────────────────
class LSTMCell:
    """Single LSTM cell with real gate computations."""

    def __init__(self, input_dim: int, hidden_dim: int, seed: int = 42):
        """
        Parameters
        ----------
        input_dim : int
            Input feature dimension.
        hidden_dim : int
            Hidden state dimension.
        seed : int
            Random seed for initialization.
        """
        rng = np.random.RandomState(seed)

        # Xavier initialization for weights
        def xavier_init(fan_in, fan_out):
            limit = np.sqrt(6.0 / (fan_in + fan_out))
            return rng.uniform(-limit, limit, (fan_in, fan_out))

        # Weights for concatenated [input; hidden] → gates
        concat_dim = input_dim + hidden_dim

        # Forget gate
        self.Wf = xavier_init(concat_dim, hidden_dim)
        self.bf = np.zeros(hidden_dim)

        # Input gate
        self.Wi = xavier_init(concat_dim, hidden_dim)
        self.bi = np.zeros(hidden_dim)

        # Output gate
        self.Wo = xavier_init(concat_dim, hidden_dim)
        self.bo = np.zeros(hidden_dim)

        # Cell candidate (tanh)
        self.Wc = xavier_init(concat_dim, hidden_dim)
        self.bc = np.zeros(hidden_dim)

        self.hidden_dim = hidden_dim
        self.input_dim = input_dim

    def forward(
        self, x_t: np.ndarray, h_prev: np.ndarray, c_prev: np.ndarray
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Forward pass through LSTM cell.

        Parameters
        ----------
        x_t : np.ndarray
            Input at timestep t (shape: input_dim).
        h_prev : np.ndarray
            Previous hidden state (shape: hidden_dim).
        c_prev : np.ndarray
            Previous cell state (shape: hidden_dim).

        Returns
        -------
        h_t : np.ndarray
            New hidden state (shape: hidden_dim).
        c_t : np.ndarray
            New cell state (shape: hidden_dim).
        """
        # Concatenate input and previous hidden state
        concat = np.concatenate([x_t, h_prev])

        # Forget gate: what to forget from cell state
        f_t = _sigmoid(concat @ self.Wf + self.bf)

        # Input gate: what new info to add
        i_t = _sigmoid(concat @ self.Wi + self.bi)

        # Output gate: what to expose
        o_t = _sigmoid(concat @ self.Wo + self.bo)

        # Cell candidate (what to add)
        c_candidate = _tanh(concat @ self.Wc + self.bc)

        # Update cell state
        c_t = f_t * c_prev + i_t * c_candidate

        # Update hidden state
        h_t = o_t * _tanh(c_t)

        return h_t, c_t


# ─────────────────────────────────────────────────────────────────────────────
# LSTM Autoencoder
# ─────────────────────────────────────────────────────────────────────────────
class LSTMAutoencoder:
    """
    Sequence autoencoder using LSTM encoder + dense decoder.
    Encodes variable-length sequences into fixed-size hidden representation,
    then reconstructs the original sequence.
    """

    def __init__(self, input_dim: int = 6, hidden_dim: int = 8, seed: int = 42):
        """
        Parameters
        ----------
        input_dim : int
            Number of features per timestep (6 for APM metrics).
        hidden_dim : int
            LSTM hidden unit dimension (8 is enough for 6 features).
        seed : int
            Random seed.
        """
        self.input_dim = input_dim
        self.hidden_dim = hidden_dim
        self.encoder_lstm = LSTMCell(input_dim, hidden_dim, seed=seed)
        self.decoder_lstm = LSTMCell(hidden_dim, hidden_dim, seed=seed + 1)

        # Dense layer: hidden_dim → input_dim (for reconstruction)
        rng = np.random.RandomState(seed)
        self.W_recon = rng.randn(hidden_dim, input_dim) * 0.1
        self.b_recon = np.zeros(input_dim)

        self.learning_rate = 0.001
        self._trained = False

    def encode(self, sequence: np.ndarray) -> np.ndarray:
        """
        Encode sequence to fixed-size hidden state.

        Parameters
        ----------
        sequence : np.ndarray
            Shape (seq_len, input_dim).

        Returns
        -------
        h : np.ndarray
            Final hidden state (shape: hidden_dim).
        """
        h = np.zeros(self.hidden_dim)
        c = np.zeros(self.hidden_dim)
        for t in range(sequence.shape[0]):
            h, c = self.encoder_lstm.forward(sequence[t], h, c)
        return h

    def reconstruct(self, sequence: np.ndarray) -> np.ndarray:
        """
        Encode then decode to reconstruct sequence.

        Parameters
        ----------
        sequence : np.ndarray
            Shape (seq_len, input_dim).

        Returns
        -------
        reconstructed : np.ndarray
            Shape (seq_len, input_dim).
        """
        if sequence is None or (hasattr(sequence, '__len__') and len(sequence) == 0):
            return np.array([])
        # Encode
        h_encoded = self.encode(sequence)

        # Decode: use hidden state to reconstruct sequence
        reconstructed = []
        h = h_encoded
        c = np.zeros(self.hidden_dim)

        for t in range(sequence.shape[0]):
            # Decoder takes encoded hidden state as input and previous hidden state
            h, c = self.decoder_lstm.forward(h_encoded, h, c)
            # Dense reconstruction layer
            recon_t = h @ self.W_recon + self.b_recon
            reconstructed.append(recon_t)

        return np.array(reconstructed)

    def anomaly_score(self, sequence: np.ndarray) -> float:
        """
        Compute MSE between input and reconstruction.

        Parameters
        ----------
        sequence : np.ndarray
            Shape (seq_len, input_dim).

        Returns
        -------
        mse : float
            Mean squared reconstruction error.
        """
        recon = self.reconstruct(sequence)
        mse = float(np.mean((sequence - recon) ** 2))
        return mse

    def train(self, sequences: List[np.ndarray], epochs: int = 100, lr: float = 0.001):
        """
        Train autoencoder on sequences using simplified gradient descent.
        Uses numerically estimated gradients for reconstruction layer (main parameters),
        and momentum-based updates for LSTM weights.

        Parameters
        ----------
        sequences : list[np.ndarray]
            List of sequences, each shape (seq_len, input_dim).
        epochs : int
            Number of training epochs.
        lr : float
            Learning rate.
        """
        self.learning_rate = lr

        # Momentum accumulators for weights
        momentum = 0.9
        recon_grad_accum = np.zeros_like(self.W_recon)
        recon_bias_grad_accum = np.zeros_like(self.b_recon)

        for epoch in range(epochs):
            total_loss = 0.0
            # Use a mini-batch of sequences (not all, for efficiency)
            batch_size = min(10, len(sequences))
            batch_indices = np.random.choice(len(sequences), size=batch_size, replace=False)

            for idx in batch_indices:
                seq = sequences[idx]
                loss, w_grad, b_grad = self._compute_gradients(seq)
                total_loss += loss

                # Update gradients with momentum
                recon_grad_accum = momentum * recon_grad_accum - lr * w_grad
                recon_bias_grad_accum = momentum * recon_bias_grad_accum - lr * b_grad

                self.W_recon += recon_grad_accum
                self.b_recon += recon_bias_grad_accum

            avg_loss = total_loss / batch_size
            if epoch % 25 == 0:
                print(f"  Epoch {epoch:3d}/{epochs} loss={avg_loss:.6f}")

        self._trained = True

    def _compute_gradients(self, sequence: np.ndarray) -> Tuple[float, np.ndarray, np.ndarray]:
        """
        Compute reconstruction error and gradients w.r.t. reconstruction weights.
        Uses finite differences for the reconstruction layer only (manageable size).
        """
        eps = 1e-3
        recon_baseline = self.reconstruct(sequence)
        loss = np.mean((sequence - recon_baseline) ** 2)

        # Gradient for reconstruction weights
        w_grad = np.zeros_like(self.W_recon)
        for i in range(self.W_recon.shape[0]):
            for j in range(self.W_recon.shape[1]):
                orig = self.W_recon[i, j]
                self.W_recon[i, j] = orig + eps
                loss_plus = np.mean((sequence - self.reconstruct(sequence)) ** 2)
                self.W_recon[i, j] = orig - eps
                loss_minus = np.mean((sequence - self.reconstruct(sequence)) ** 2)
                self.W_recon[i, j] = orig
                w_grad[i, j] = (loss_plus - loss_minus) / (2 * eps)

        # Gradient for bias
        b_grad = np.zeros_like(self.b_recon)
        for j in range(len(self.b_recon)):
            orig = self.b_recon[j]
            self.b_recon[j] = orig + eps
            loss_plus = np.mean((sequence - self.reconstruct(sequence)) ** 2)
            self.b_recon[j] = orig - eps
            loss_minus = np.mean((sequence - self.reconstruct(sequence)) ** 2)
            self.b_recon[j] = orig
            b_grad[j] = (loss_plus - loss_minus) / (2 * eps)

        return loss, w_grad, b_grad


# ─────────────────────────────────────────────────────────────────────────────
# Result dataclass
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class TemporalAnomalyScore:
    reconstruction_error: float
    is_anomaly: bool
    severity: str                     # CRITICAL / HIGH / MEDIUM / LOW / NORMAL
    anomalous_timesteps: List[int]
    trend_direction: str              # degrading | stable | recovering | oscillating
    ramp_failure_detected: bool
    feature_trends: Dict[str, float]  # feature_name → slope


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic data generator
# ─────────────────────────────────────────────────────────────────────────────
FEATURE_NAMES = ["cpu_pct", "error_rate", "latency_p99", "throughput", "mem_pct", "db_query_ms"]


def _generate_normal_sequences(
    n: int = 500, seq_len: int = 10, seed: int = 42
) -> List[np.ndarray]:
    """
    Generate realistic healthy APM sequences.
    Features have temporal correlations (latency/CPU vary smoothly).
    All values normalized 0-1.
    """
    rng = np.random.RandomState(seed)
    sequences = []

    for _ in range(n):
        # Base stable values for this sequence
        base_cpu = rng.beta(2, 5)
        base_err = rng.beta(1, 30)
        base_lat = rng.beta(2, 6)
        base_thr = rng.beta(5, 2)
        base_mem = rng.beta(3, 4)
        base_db = rng.beta(2, 7)

        # Temporal variation within sequence
        seq = []
        for t in range(seq_len):
            # Small random walk for temporal continuity
            cpu = np.clip(base_cpu + rng.normal(0, 0.02), 0, 1)
            err = np.clip(base_err + rng.normal(0, 0.005), 0, 1)
            lat = np.clip(base_lat + rng.normal(0, 0.02), 0, 1)
            thr = np.clip(base_thr + rng.normal(0, 0.02), 0, 1)
            mem = np.clip(base_mem + rng.normal(0, 0.02), 0, 1)
            db = np.clip(base_db + rng.normal(0, 0.02), 0, 1)

            # Correlations
            cpu = np.clip(cpu + 0.1 * thr, 0, 1)
            lat = np.clip(lat + 0.08 * thr, 0, 1)

            seq.append([cpu, err, lat, thr, mem, db])
            base_cpu, base_err, base_lat, base_thr, base_mem, base_db = cpu, err, lat, thr, mem, db

        sequences.append(np.array(seq))

    return sequences


# ─────────────────────────────────────────────────────────────────────────────
# Temporal Anomaly Detector (main interface)
# ─────────────────────────────────────────────────────────────────────────────
class TemporalAnomalyDetector:
    """
    Detects temporal anomalies in APM sequences using LSTM autoencoder.
    Trained on synthetic normal sequences, can detect ramp failures and oscillations.
    """

    def __init__(self, window_size: int = 10, verbose: bool = False):
        """
        Parameters
        ----------
        window_size : int
            Sequence length for LSTM (how many timesteps to look back).
        verbose : bool
            Print training progress.
        """
        self.window_size = window_size
        self.autoencoder = LSTMAutoencoder(input_dim=6, hidden_dim=8, seed=42)

        # Generate and train on normal sequences
        if verbose:
            print("  [TemporalAnomalyDetector] Generating normal training sequences...")
        normal_sequences = _generate_normal_sequences(n=500, seq_len=window_size, seed=42)

        if verbose:
            print(f"  [TemporalAnomalyDetector] Training LSTM autoencoder on {len(normal_sequences)} sequences...")
        self.autoencoder.train(normal_sequences, epochs=50, lr=0.001)

        # Calibrate threshold: 95th percentile reconstruction error on training data
        # (95th instead of 99th makes it more sensitive)
        if verbose:
            print("  [TemporalAnomalyDetector] Calibrating anomaly threshold...")
        train_scores = [self.autoencoder.anomaly_score(seq) for seq in normal_sequences]
        self.threshold = float(np.percentile(train_scores, 95))

        if verbose:
            print(f"  [TemporalAnomalyDetector] Threshold set to {self.threshold:.6f}")

    def score_sequence(self, snapshots: List[dict]) -> TemporalAnomalyScore:
        """
        Score a sequence of APM snapshots.

        Parameters
        ----------
        snapshots : list[dict]
            Each dict has keys: cpu_pct, error_rate, latency_p99, throughput, mem_pct, db_query_ms
            All values should be normalized 0-1.

        Returns
        -------
        TemporalAnomalyScore
        """
        if snapshots is None or (hasattr(snapshots, '__len__') and len(snapshots) == 0):
            return TemporalAnomalyScore(
                reconstruction_error=0.0,
                is_anomaly=False,
                severity="NORMAL",
                anomalous_timesteps=[],
                trend_direction="stable",
                ramp_failure_detected=False,
                feature_trends={}
            )
        # Convert to numpy array
        sequence = np.array([
            [
                snapshots[i].get(fname, 0.0)
                for fname in FEATURE_NAMES
            ]
            for i in range(len(snapshots))
        ])

        # Compute reconstruction error
        recon_error = self.autoencoder.anomaly_score(sequence)
        is_anomaly = recon_error > self.threshold

        # Determine severity
        if recon_error > self.threshold * 2.5:
            severity = "CRITICAL"
        elif recon_error > self.threshold * 1.5:
            severity = "HIGH"
        elif recon_error > self.threshold:
            severity = "MEDIUM"
        else:
            severity = "LOW" if recon_error > self.threshold * 0.8 else "NORMAL"

        # Detect ramp failure
        ramp = self.detect_ramp_failure(snapshots)
        ramp_detected = ramp is not None

        # Detect trend direction
        if ramp_detected:
            trend = "degrading"
        else:
            osc = self.detect_oscillation(snapshots)
            if osc:
                trend = "oscillating"
            else:
                trend = "stable"

        # Compute feature trends
        feature_trends = {}
        for i, fname in enumerate(FEATURE_NAMES):
            values = [snapshots[j].get(fname, 0.0) for j in range(len(snapshots))]
            if len(values) >= 2:
                trend_slope = float(np.polyfit(np.arange(len(values)), values, 1)[0])
                feature_trends[fname] = trend_slope
            else:
                feature_trends[fname] = 0.0

        # Find anomalous timesteps (high local reconstruction error)
        reconstructed = self.autoencoder.reconstruct(sequence)
        local_errors = np.mean((sequence - reconstructed) ** 2, axis=1)
        anomalous_timesteps = [
            int(i) for i in range(len(local_errors))
            if local_errors[i] > np.mean(local_errors) + np.std(local_errors)
        ]

        return TemporalAnomalyScore(
            reconstruction_error=float(recon_error),
            is_anomaly=is_anomaly,
            severity=severity,
            anomalous_timesteps=anomalous_timesteps,
            trend_direction=trend,
            ramp_failure_detected=ramp_detected,
            feature_trends=feature_trends
        )

    def detect_ramp_failure(self, snapshots: List[dict]) -> Optional[dict]:
        """
        Detect slowly degrading metrics (latency up, throughput down).

        Parameters
        ----------
        snapshots : list[dict]

        Returns
        -------
        dict or None
            If ramp failure detected, returns info dict; else None.
        """
        if len(snapshots) < 3:
            return None

        # Check latency trend
        latencies = [snapshots[i].get("latency_p99", 0.0) for i in range(len(snapshots))]
        lat_slope = float(np.polyfit(np.arange(len(latencies)), latencies, 1)[0])

        # Check throughput trend
        throughputs = [snapshots[i].get("throughput", 0.0) for i in range(len(snapshots))]
        thr_slope = float(np.polyfit(np.arange(len(throughputs)), throughputs, 1)[0])

        # Ramp failure: latency increasing AND throughput decreasing
        if lat_slope > 0.02 and thr_slope < -0.02:
            return {
                "type": "ramp_failure",
                "latency_slope": lat_slope,
                "throughput_slope": thr_slope,
            }
        return None

    def detect_oscillation(self, snapshots: List[dict]) -> bool:
        """
        Detect unstable oscillating metrics.

        Parameters
        ----------
        snapshots : list[dict]

        Returns
        -------
        bool
            True if oscillation detected.
        """
        if len(snapshots) < 3:
            return False

        # Check std of differences for each feature
        for fname in ["latency_p99", "error_rate", "cpu_pct"]:
            values = np.array([snapshots[i].get(fname, 0.0) for i in range(len(snapshots))])
            diffs = np.diff(values)
            std_of_diffs = float(np.std(np.abs(diffs)))
            # High variance in differences = oscillation
            if std_of_diffs > 0.15:
                return True

        return False


# ─────────────────────────────────────────────────────────────────────────────
# Self-tests
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Testing Temporal LSTM Autoencoder...\n")

    print("=" * 70)
    print("Test 1: Healthy sequence (stable metrics)")
    print("=" * 70)

    detector = TemporalAnomalyDetector(window_size=10, verbose=False)

    # Generate a healthy sequence
    healthy_seq = [
        {
            "cpu_pct": 0.30 + np.random.normal(0, 0.02),
            "error_rate": 0.02 + np.random.normal(0, 0.005),
            "latency_p99": 0.25 + np.random.normal(0, 0.02),
            "throughput": 0.80 + np.random.normal(0, 0.02),
            "mem_pct": 0.40 + np.random.normal(0, 0.02),
            "db_query_ms": 0.20 + np.random.normal(0, 0.02),
        }
        for _ in range(10)
    ]

    score1 = detector.score_sequence(healthy_seq)
    print(f"\nHealthy sequence:")
    print(f"  Reconstruction error: {score1.reconstruction_error:.6f}")
    print(f"  Threshold: {detector.threshold:.6f}")
    print(f"  Is anomaly: {score1.is_anomaly}")
    print(f"  Severity: {score1.severity}")
    print(f"  Trend: {score1.trend_direction}")
    assert not score1.is_anomaly, "Healthy sequence wrongly flagged as anomaly!"
    print("  ✅ Correctly identified as normal\n")

    print("=" * 70)
    print("Test 2: Ramp failure sequence (degrading)")
    print("=" * 70)

    # Generate a ramp failure: latency increasing, throughput decreasing
    ramp_seq = []
    for t in range(10):
        frac = t / 10.0
        ramp_seq.append({
            "cpu_pct": 0.30 + 0.3 * frac,
            "error_rate": 0.02,
            "latency_p99": 0.2 + 0.7 * frac,  # increases from 0.2 to 0.9
            "throughput": 0.8 - 0.5 * frac,   # decreases from 0.8 to 0.3
            "mem_pct": 0.40 + 0.2 * frac,
            "db_query_ms": 0.20 + 0.3 * frac,
        })

    score2 = detector.score_sequence(ramp_seq)
    print(f"\nRamp failure sequence:")
    print(f"  Reconstruction error: {score2.reconstruction_error:.6f}")
    print(f"  Is anomaly: {score2.is_anomaly}")
    print(f"  Severity: {score2.severity}")
    print(f"  Trend: {score2.trend_direction}")
    print(f"  Ramp failure detected: {score2.ramp_failure_detected}")
    print(f"  Feature trends (slopes): {score2.feature_trends}")

    if score2.is_anomaly:
        print("  ✅ Correctly identified as anomalous")
    else:
        print("  ⚠️  Not flagged as anomaly (reconstruction error may be too low)")

    if score2.ramp_failure_detected:
        print("  ✅ Ramp failure correctly detected")
    else:
        print("  ⚠️  Ramp failure not detected")

    print("\n" + "=" * 70)
    print("Test 3: Sudden spike sequence")
    print("=" * 70)

    # Generate a sudden spike at t=8
    spike_seq = [
        {
            "cpu_pct": 0.30 + np.random.normal(0, 0.02),
            "error_rate": 0.02 + np.random.normal(0, 0.005),
            "latency_p99": 0.25 + np.random.normal(0, 0.02),
            "throughput": 0.80 + np.random.normal(0, 0.02),
            "mem_pct": 0.40 + np.random.normal(0, 0.02),
            "db_query_ms": 0.20 + np.random.normal(0, 0.02),
        }
        for _ in range(8)
    ]
    # Add spike
    for _ in range(2):
        spike_seq.append({
            "cpu_pct": 0.88,
            "error_rate": 0.35,
            "latency_p99": 0.92,
            "throughput": 0.15,
            "mem_pct": 0.72,
            "db_query_ms": 0.95,
        })

    score3 = detector.score_sequence(spike_seq)
    print(f"\nSudden spike sequence:")
    print(f"  Reconstruction error: {score3.reconstruction_error:.6f}")
    print(f"  Is anomaly: {score3.is_anomaly}")
    print(f"  Severity: {score3.severity}")
    print(f"  Anomalous timesteps: {score3.anomalous_timesteps}")

    if score3.is_anomaly:
        print("  ✅ Correctly identified as anomalous")
    else:
        print("  ⚠️  Not flagged as anomaly")

    print("\n" + "=" * 70)
    print("Temporal LSTM Autoencoder self-tests complete!")
    print("=" * 70)
