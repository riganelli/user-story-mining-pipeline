"""
Chat page — Q&A on the pipeline statistics through a local LLM (Ollama).
"""
import json

import requests
import streamlit as st

from core.ui import inject_css, page_hero
inject_css()

from core.llm_context import build_context, context_to_prompt_string


st.set_page_config(page_title="Chat", page_icon="💬", layout="wide")

if "data" not in st.session_state or st.session_state.data is None:
    st.warning("⚠️ No output loaded. Go back to **Home** and load a folder.")
    st.stop()

data = st.session_state.data

page_hero("💬 Chat on statistics", "Ask questions in natural language about the loaded run. Answered by a local Llama.")


# Sidebar config.
with st.sidebar:
    st.header("⚙️ LLM config")
    # Endpoint fixed to the local default, not shown to the user.
    endpoint = "http://localhost:11434"
    model = st.text_input(
        "Model",
        value="qwen2.5:7b",
        help="Name of the installed Ollama model (e.g. qwen2.5:7b, llama3.1:8b).",
    )
    temperature = st.slider(
        "Temperature",
        min_value=0.0, max_value=1.0, value=0.2, step=0.1,
        help="Lower values = more deterministic and factual answers.",
    )
    st.divider()
    if st.button("🗑️ Clear chat", use_container_width=True):
        st.session_state.chat_history = []
        st.rerun()
    with st.expander("🔍 Show context sent to the LLM"):
        ctx_preview = build_context(data)
        st.code(context_to_prompt_string(ctx_preview), language="json")

# Chat state.
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []

# Suggestions.
if not st.session_state.chat_history:
    st.markdown("##### 💡 Try asking")
    suggestions = [
        "Which discovery strategy is the most efficient in terms of time per repo?",
        "How many complete Connextra user stories were found, and from which strategies?",
        "Why does TAG have so many issues rejected by the regex?",
        "What is the AI validation rate for each strategy?",
        "Summarize the quality of the final dataset in 3 sentences.",
    ]
    cols = st.columns(2)
    for i, sug in enumerate(suggestions):
        with cols[i % 2]:
            if st.button(sug, key=f"sug_{i}", use_container_width=True):
                st.session_state.pending_question = sug
                st.rerun()

# Render history.
for msg in st.session_state.chat_history:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# User input.
user_input = st.chat_input("Ask a question about the run...")

# Use a pending question from the suggestions, if any.
if "pending_question" in st.session_state:
    user_input = st.session_state.pending_question
    del st.session_state.pending_question

# Call LLM.
def call_ollama(prompt: str, system: str, endpoint: str, model: str, temperature: float) -> str:
    """Call Ollama /api/chat and return the response text."""
    url = f"{endpoint.rstrip('/')}/api/chat"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "options": {"temperature": temperature},
    }
    r = requests.post(url, json=payload, timeout=120)
    r.raise_for_status()
    data = r.json()
    return data.get("message", {}).get("content", "").strip()


SYSTEM_PROMPT = """You are an expert data-analysis assistant that helps interpret
the results of a user story discovery pipeline on GitHub.

RULES:
- ALWAYS answer in English.
- Rely EXCLUSIVELY on the data in the JSON context you are given.
- If a question cannot be answered from the provided data, say so clearly.
- Be concise and concrete: use specific numbers when possible.
- Do not make up data.

PAY ATTENTION to relative metrics:
- "time per repo" or "cost per repo" → use `seconds_per_accepted_repo`, NOT total time.
- "validation rate" → use the pre-computed `*_pct` fields.
- "efficiency" → always look at normalized metrics (per-repo, percentages), never absolute values.
- If the question requires a calculation not already present in the JSON, say so instead of making it up.
- When the question asks for "the most efficient", "the fastest", "the best" on a metric, MENTALLY list all values, sort them, and pick the one with the minimum value (or maximum, depending on the case). Do NOT rely on the first one that comes to mind.

STYLE:
- Direct answer in the first two lines.
- Details and numbers after, if useful.
- Avoid long preambles like "To answer this question...".
- Use markdown (bold, lists) only if useful for clarity.
"""

if user_input:
    st.session_state.chat_history.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    ctx = build_context(data)
    ctx_str = context_to_prompt_string(ctx)
    full_prompt = (
        f"Here is the aggregated run data (JSON):\n\n```json\n{ctx_str}\n```\n\n"
        f"User question: {user_input}"
    )

    with st.chat_message("assistant"):
        with st.spinner(f"🤖 {model} is thinking..."):
            try:
                answer = call_ollama(
                    prompt=full_prompt,
                    system=SYSTEM_PROMPT,
                    endpoint=endpoint,
                    model=model,
                    temperature=temperature,
                )
            except requests.exceptions.ConnectionError:
                answer = (
                    "❌ **Cannot connect to Ollama.** "
                    f"Make sure it is running on `{endpoint}` "
                    "(launch `ollama serve` in a terminal)."
                )
            except requests.exceptions.HTTPError as e:
                answer = f"❌ **HTTP error from the Ollama server:** {e}"
            except Exception as e:
                answer = f"❌ **Unexpected error:** {type(e).__name__}: {e}"
        st.markdown(answer)

    st.session_state.chat_history.append({"role": "assistant", "content": answer})