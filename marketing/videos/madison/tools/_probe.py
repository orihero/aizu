import os
import sys

os.environ.setdefault("HF_HOME", r"D:\AI\hf_cache")
from faster_whisper import WhisperModel

model = WhisperModel("large-v3", device="cpu", compute_type="int8")
for path in sys.argv[1:]:
    segs, _ = model.transcribe(path, language="en", vad_filter=False, beam_size=5,
                               condition_on_previous_text=False)
    text = " ".join(s.text.strip() for s in segs).strip()
    print("{:<44} {}".format(os.path.basename(path), text or "(nothing)"))
