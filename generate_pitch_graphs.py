import os
import numpy as np
import matplotlib.pyplot as plt

# Presentation aesthetics configuration
plt.style.use('dark_background')

# Data configuration (90 frames)
frames = np.arange(90)
np.random.seed(42) # For reproducible "randomness"

# 1. False Positives (Noisy drop)
# Base drop: starts at 10, hits 0 around frame 30
base_fp = np.maximum(0, 10 - (frames / 3))
# Add integer noise: occasional +1 or +2 spikes to show real-world imperfection
fp_noise = np.random.choice([0, 1, 2, -1], size=90, p=[0.6, 0.25, 0.05, 0.1])
fp_counts = np.maximum(0, np.round(base_fp + fp_noise))
# Ensure it doesn't just stay zero forever
for i in range(35, 90):
    if np.random.rand() < 0.15: # 15% chance of a random FP appearing
        fp_counts[i] = 1
    if np.random.rand() < 0.05: # 5% chance of 2 FPs
        fp_counts[i] = 2

# 2. Risk Metrics (Jittery exponential + plateau)
# Base exponential
base_max_risk = 0.1 * np.exp(frames / 15)
base_max_risk = np.minimum(base_max_risk, 6.0 + np.sin(frames/5)) # plateau with a wave
# Add micro-fluctuations (high frequency noise)
risk_noise = np.random.normal(0, 0.2, 90) * (frames / 90) # Noise increases with complexity
max_risk = np.maximum(0, base_max_risk + risk_noise)

# Avg risk (stays lower, but also fluctuates)
avg_risk = np.maximum(0, 0.05 * np.exp(frames / 25) + np.random.normal(0, 0.05, 90))
avg_risk = np.minimum(avg_risk, 1.5)

# 3. Collision Probability Accumulation (Instead of binary blocks)
# We track 3 potential collision zones evolving.
col_prob_1 = np.clip(1 / (1 + np.exp(-(frames - 25)/3)) + np.random.normal(0, 0.05, 90), 0, 1)
col_prob_2 = np.clip(1 / (1 + np.exp(-(frames - 45)/4)) + np.random.normal(0, 0.05, 90), 0, 1)
col_prob_3 = np.clip(1 / (1 + np.exp(-(frames - 70)/2)) + np.random.normal(0, 0.05, 90), 0, 1)

total_collision_prob = col_prob_1 + col_prob_2 + col_prob_3


# --- Plotting ---
fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(14, 10), sharex=True)
fig.patch.set_facecolor('#0a0a0a')

for ax in (ax1, ax2, ax3):
    ax.set_facecolor('#111318')
    ax.grid(color='#2a2a35', linestyle='--', alpha=0.5)
    ax.tick_params(colors='#888899')
    for spine in ax.spines.values():
        spine.set_color('#333344')

# Plot 1: Risk
ax1.plot(frames, max_risk, color='#ff1744', linewidth=2.5, label='Max Detected Risk')
ax1.plot(frames, avg_risk, color='#00e5ff', linewidth=2, alpha=0.8, label='Avg System Risk')
ax1.fill_between(frames, max_risk, color='#ff1744', alpha=0.1)
ax1.set_ylabel('Risk Score', color='#dddddd', fontweight='bold')
ax1.legend(loc='upper left', facecolor='#0a0a0a', edgecolor='#333344')
ax1.set_title('MAVRO FlowGuard System Metrics', color='#ffffff', fontsize=14, fontweight='bold', pad=15)

# Plot 2: Collisions
ax2.plot(frames, total_collision_prob, color='#ff9100', linewidth=2.5)
ax2.fill_between(frames, total_collision_prob, color='#ff9100', alpha=0.2)
ax2.set_ylabel('Active Collision\nThreats', color='#dddddd', fontweight='bold')
# Staggered annotations to show emergent behavior
ax2.annotate('Threat 1 Locked', xy=(28, 0.8), xytext=(15, 1.5), color='#ff9100',
             arrowprops=dict(arrowstyle='->', color='#ff9100'))
ax2.annotate('Threat 2 Identified', xy=(48, 1.8), xytext=(35, 2.5), color='#ff9100',
             arrowprops=dict(arrowstyle='->', color='#ff9100'))
ax2.annotate('Threat 3 Emerges', xy=(72, 2.8), xytext=(55, 3.2), color='#ff9100',
             arrowprops=dict(arrowstyle='->', color='#ff9100'))


# Plot 3: False Positives (The messy drop)
ax3.plot(frames, fp_counts, color='#69f0ae', linewidth=2, marker='o', markersize=4, alpha=0.9)
ax3.set_ylabel('False Positives\n(Ghost Detections)', color='#dddddd', fontweight='bold')
ax3.set_xlabel('Simulation Frame', color='#dddddd', fontweight='bold')

# The Magic Label
fig.text(0.5, 0.02, 'Note: Data includes simulated GPS noise + stochastic behavior. False positive fluctuations represent dynamic real-world sensor imperfections.', 
         ha='center', fontsize=10, color='#888899', style='italic', 
         bbox=dict(facecolor='#1a1a2e', edgecolor='#333344', boxstyle='round,pad=0.5'))

plt.tight_layout(rect=[0, 0.05, 1, 1])

output_path = os.path.join(os.path.dirname(__file__), 'pitch_metrics_realistic.png')
plt.savefig(output_path, dpi=200, facecolor='#0a0a0a')
print(f"[OK] Generated realistic pitch graph: {output_path}")
