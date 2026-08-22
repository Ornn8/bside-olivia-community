import os, sys, subprocess, json, glob

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, 'CosyVoice'))  # cosyvoice 包

from cosyvoice.cli.cosyvoice import AutoModel

MODEL_DIR = os.path.join(ROOT, 'CosyVoice', 'pretrained_models', 'Fun-CosyVoice3-0.5B')
REF_MP3 = os.path.join(ROOT, 'olivia_assets', '林离语音素材', 'BV113ur6EEkL_视频回复.mp3')
REF_FULL_WAV = os.path.join(ROOT, 'output_audio', 'bv113_16k.wav')
REF_WAV = os.path.join(ROOT, 'output_audio', 'bv113_prompt_4p85s.wav')
OUT_DIR = os.path.join(ROOT, 'output_audio', 'cosyvoice3')
os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(os.path.dirname(REF_WAV), exist_ok=True)

REF_TEXT = "你這個消息來得真巧，其實過段時間，我也要出發去旅行。"

def to16k(mp3, wav):
    ff = None
    for p in glob.glob(os.path.join(ROOT, 'CosyVoice', 'venv', 'Lib', 'site-packages', 'imageio_ffmpeg', 'binaries', 'ffmpeg*.exe')):
        ff = p
    if not ff:
        import imageio_ffmpeg
        ff = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run([ff, '-y', '-i', mp3, '-ac', '1', '-ar', '16000', wav], check=True, capture_output=True)
    print('参考音频 16k:', wav)

def make_prompt(full_wav, prompt_wav, seconds=4.85):
    import soundfile as sf
    audio, sample_rate = sf.read(full_wav, dtype='float32')
    sf.write(prompt_wav, audio[:int(seconds * sample_rate)], sample_rate)
    print(f'参考片段 {seconds}s:', prompt_wav)

def main():
    # 1. 转 16k
    if not os.path.exists(REF_FULL_WAV):
        to16k(REF_MP3, REF_FULL_WAV)
    if not os.path.exists(REF_WAV):
        make_prompt(REF_FULL_WAV, REF_WAV)

    # 2. 加载模型
    use_fp16 = os.environ.get('COSYVOICE_FP16', '1') != '0'
    print(f'加载 CosyVoice3 ... fp16={use_fp16}')
    cosyvoice = AutoModel(model_dir=MODEL_DIR, fp16=use_fp16)
    print('加载完成')

    # 3. 零样本克隆（模型输入格式：prompt_text 需要前缀）
    tts_text = sys.argv[1] if len(sys.argv) > 1 else "嗨，今天过得怎么样？我一直在等你写信给我呢。听说外面的世界下雪了，你那边冷吗？记得多穿一点。"
    prompt_text = 'You are a helpful assistant.<|endofprompt|>' + REF_TEXT
    print(f'合成: {tts_text}')
    out = os.path.join(OUT_DIR, 'test_zs.wav')
    import soundfile as sf
    for i, chunk in enumerate(cosyvoice.inference_zero_shot(tts_text, prompt_text, REF_WAV, stream=False)):
        speech = chunk['tts_speech'].detach().cpu().float().numpy().squeeze()
        # This in-the-wild prompt makes CosyVoice3 echo its final prompt phrase
        # before the requested text.  The deterministic boundary is followed by
        # a clean pause; retain 0.28 s of that pause before the target greeting.
        leading_trim = int(4.4 * cosyvoice.sample_rate)
        speech = speech[leading_trim:]
        sf.write(out, speech, cosyvoice.sample_rate, format='WAV', subtype='PCM_16')
        break  # 非流式只取第一块
    print('保存:', out)

if __name__ == '__main__':
    main()
