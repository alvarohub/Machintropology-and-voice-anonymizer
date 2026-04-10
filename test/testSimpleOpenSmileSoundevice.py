import sounddevice as sd
import opensmile
import numpy as np
import pandas as pd

# 1. Configuration
SAMPLING_RATE = 16000  # Standard for openSMILE
DURATION = 5  # Seconds
CHANNELS = 1

# 2. Initialize openSMILE
smile = opensmile.Smile(
    feature_set=opensmile.FeatureSet.GeMAPSv01b,
    feature_level=opensmile.FeatureLevel.Functionals,
)

print("Recording...")

# 3. Record audio from microphone
audio_data = sd.rec(int(DURATION * SAMPLING_RATE), 
                    samplerate=SAMPLING_RATE, 
                    channels=CHANNELS, 
                    dtype='float32')
sd.wait()  # Wait until recording is finished
audio_data = audio_data.flatten()

print("Recording finished. Analyzing...")

# 4. Extract features
features = smile.process_signal(audio_data, SAMPLING_RATE)

# 5. Print results
print("Extracted features:")
print(features)
#To see all feature columns: 
print(features.columns)
