"""
Quick probe: does qwen3.6:27b-q8 via ollama.chat(think=True) populate
part.message.thinking, or does thinking arrive inside part.message.content?
"""
import ollama

MODEL = "qwen3.6:27b-q8"
PROMPT = "What is 2+2? Think briefly then answer."

thinking_chunks = []
content_chunks = []
think_in_content = False

print(f"Probing {MODEL} via native ollama.chat(think=True)...\n")

for part in ollama.chat(
    model=MODEL,
    messages=[{"role": "user", "content": PROMPT}],
    think=True,
    stream=True,
    options={"num_ctx": 4096},
):
    thinking = getattr(part.message, "thinking", None)
    content = part.message.content or ""

    if thinking:
        thinking_chunks.append(thinking)
    if content:
        content_chunks.append(content)
    if "<think>" in content:
        think_in_content = True

thinking_text = "".join(thinking_chunks)
content_text = "".join(content_chunks)

print("=== Results ===")
print(f"part.message.thinking populated : {bool(thinking_text)}")
print(f"<think> tags in content         : {think_in_content}")
print(f"thinking length (chars)         : {len(thinking_text)}")
print(f"content length (chars)          : {len(content_text)}")
print()
if thinking_text:
    print(f"thinking preview : {thinking_text[:200]!r}")
if content_text:
    print(f"content preview  : {content_text[:200]!r}")
print()

if thinking_text and not think_in_content:
    print("VERDICT: native_ollama=True will work — thinking is in message.thinking field.")
elif think_in_content:
    print("VERDICT: native_ollama=False is correct — thinking arrives inside content as <think> tags.")
else:
    print("VERDICT: unclear — no thinking detected at all (model may have skipped reasoning).")
