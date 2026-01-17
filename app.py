<<<<<<< HEAD
import os
import re
import json
import math
from typing import List, Dict, Any

from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
from PIL import Image
import google.generativeai as genai

load_dotenv()

app = Flask(__name__)
CORS(app)

API_KEY = os.getenv("GEMINI_API_KEY")
if not API_KEY:
    raise RuntimeError("GEMINI_API_KEY missing in .env")

genai.configure(api_key=API_KEY)

PROMPT_TEXT = (
    "Analyze the food in this image. "
    "Assume a standard serving size. "
    "Identify the dish name. "
    "List the main ingredients. "
    "Estimate the calorie range (kcal). "
    "Estimate macronutrients for the serving: "
    "protein (g), carbohydrates (g), and fat (g). "
    "If the image is unclear, state uncertainty clearly. "
    "Respond briefly in clear bullet points."
)

@app.get("/ping")
def ping():
    return jsonify({"ok": True})


# --------------------- FOOD PARSING ---------------------

def _avg_from_range_text(text: str):
    """
    '30-40' -> 35
    '30 – 40' -> 35
    '30' -> 30
    """
    if text is None:
        return None
    t = text.strip()
    m = re.search(r"(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)", t)
    if m:
        a = float(m.group(1))
        b = float(m.group(2))
        return (a + b) / 2.0
    m2 = re.search(r"(\d+(?:\.\d+)?)", t)
    if m2:
        return float(m2.group(1))
    return None

def extract_fields(raw_text: str):
    text = (raw_text or "").strip()
    low = text.lower()

    dish_name = None
    m = re.search(r"dish name\s*[:\-]\s*(.+)", text, flags=re.IGNORECASE)
    if m:
        dish_name = m.group(1).strip()
        dish_name = re.split(r"\n|\*", dish_name)[0].strip()

    kcal_range = None
    m = re.search(r"(\d+)\s*[-–]\s*(\d+)\s*kcal", low)
    if m:
        kcal_range = [int(m.group(1)), int(m.group(2))]
    else:
        m = re.search(r"(\d+)\s*(?:to)\s*(\d+)\s*kcal", low)
        if m:
            kcal_range = [int(m.group(1)), int(m.group(2))]

    protein_g = None
    m = re.search(
        r"protein\s*[:\-]?\s*([0-9]+(?:\.[0-9]+)?(?:\s*[-–]\s*[0-9]+(?:\.[0-9]+)?)?)\s*g",
        low
    )
    if m:
        protein_g = _avg_from_range_text(m.group(1))

    carbs_g = None
    m = re.search(
        r"(?:carbs|carbohydrates|carbohydrate)\s*[:\-]?\s*([0-9]+(?:\.[0-9]+)?(?:\s*[-–]\s*[0-9]+(?:\.[0-9]+)?)?)\s*g",
        low
    )
    if m:
        carbs_g = _avg_from_range_text(m.group(1))

    fat_g = None
    m = re.search(
        r"fat\s*[:\-]?\s*([0-9]+(?:\.[0-9]+)?(?:\s*[-–]\s*[0-9]+(?:\.[0-9]+)?)?)\s*g",
        low
    )
    if m:
        fat_g = _avg_from_range_text(m.group(1))

    return {
        "dishName": dish_name,
        "kcalRange": kcal_range,
        "proteinG": protein_g,
        "carbsG": carbs_g,
        "fatG": fat_g,
    }

@app.post("/api/analyze_food")
def analyze_food():
    if "image" not in request.files:
        return jsonify({"error": "Missing file field 'image'"}), 400

    file = request.files["image"]
    if not file.filename:
        return jsonify({"error": "Empty filename"}), 400

    try:
        img = Image.open(file.stream).convert("RGB")
    except Exception:
        return jsonify({"error": "Invalid image"}), 400

    model = genai.GenerativeModel("gemini-2.0-flash")

    try:
        result = model.generate_content([PROMPT_TEXT, img])
        raw_text = (result.text or "").strip()
    except Exception as e:
        return jsonify({"error": "Gemini request failed", "details": str(e)}), 500

    parsed = extract_fields(raw_text)

    return jsonify({
        "rawText": raw_text,
        "parsed": parsed
    })

# --------------------- WORKOUT PLAN (NEW) ---------------------

WORKOUT_PROMPT = """
You are a workout planner.
Goal: Burn approximately targetCalories kcal for a user with weightKg kg.

You will receive a list of candidate exercises with fields: id, name, met.
Use only exercises from the candidates list.
Return STRICT JSON ONLY in this schema (no markdown, no extra text):

{
  "planTitle": "string",
  "items": [
    {"id":"string","minutes": number, "sets": number|null, "reps": number|null, "note":"string"}
  ]
}

Rules:
- 3 to 6 items total.
- minutes must be between 3 and 20 per item.
- Prefer a mix: 1-2 strength + 1-2 cardio + optional core.
- If you are unsure, still output valid JSON.
"""

def kcal_burned(met: float, weightKg: float, minutes: float) -> float:
    # kcal = MET * weightKg * minutes / 60
    return (met * weightKg * minutes) / 60.0


def fallback_plan(targetCalories: float, weightKg: float, candidates):
    """
    Gemini yoksa (quota bitti vb.) tamamen local deterministik plan üretir.
    Yüksek MET'ten başlayıp 3-6 egzersizle hedefe yaklaşır.
    """
    cands = [c for c in candidates if float(c.get("met", 0)) > 0]
    cands.sort(key=lambda x: float(x["met"]), reverse=True)

    items = []
    remaining = float(targetCalories)

    # 4 item hedefleyelim
    for c in cands[:10]:
        if remaining <= 0:
            break

        met = float(c["met"])

        minutes = (remaining * 60.0) / (met * weightKg)
        minutes = max(4.0, min(12.0, minutes))
        burned = kcal_burned(met, weightKg, minutes)

        items.append({
            "id": c["id"],
            "minutes": round(minutes, 1),
            "sets": None,
            "reps": None,
            "note": "Auto plan (fallback)."
        })

        remaining -= burned
        if len(items) >= 5:
            break

    return {
        "planTitle": "AI Workout Plan",
        "items": items
    }


def safe_json_extract(text: str):
    """
    Gemini bazen JSON dışı yazarsa içinden JSON'u çekmeye çalışır.
    """
    t = (text or "").strip()

    try:
        return json.loads(t)
    except Exception:
        pass

    start = t.find("{")
    end = t.rfind("}")
    if start != -1 and end != -1 and end > start:
        chunk = t[start:end + 1]
        return json.loads(chunk)

    raise ValueError("Gemini did not return valid JSON")


@app.post("/api/workout_plan")
def workout_plan():
    data = request.get_json(silent=True) or {}

    targetCalories = float(data.get("targetCalories", 0) or 0)
    weightKg = float(data.get("weightKg", 0) or 0)
    candidates = data.get("candidates", []) or []

    if targetCalories <= 0 or weightKg <= 0:
        return jsonify({"error": "targetCalories and weightKg must be > 0"}), 400

    if not isinstance(candidates, list) or len(candidates) == 0:
        return jsonify({"error": "candidates list is required"}), 400

    try:
        model = genai.GenerativeModel("gemini-2.0-flash")
        payload = {
            "targetCalories": targetCalories,
            "weightKg": weightKg,
            "candidates": candidates
        }

        result = model.generate_content([WORKOUT_PROMPT, json.dumps(payload)])
        raw = (result.text or "").strip()
        plan_json = safe_json_extract(raw)

    except Exception as e:
        plan_json = fallback_plan(targetCalories, weightKg, candidates)
        plan_json["_note"] = f"Gemini unavailable, used fallback. ({str(e)})"

    by_id = {c.get("id"): c for c in candidates}

    out_items = []
    total_minutes = 0.0

    for it in (plan_json.get("items") or []):
        ex_id = (it.get("id") or "").strip()
        minutes = float(it.get("minutes") or 0)

        if not ex_id or minutes <= 0:
            continue

        cand = by_id.get(ex_id)
        if not cand:
            continue

        met = float(cand.get("met") or 0)
        name = str(cand.get("name") or ex_id)

        minutes = max(3.0, min(20.0, minutes))
        total_minutes += minutes

        out_items.append({
            "id": ex_id,
            "name": name,
            "met": met,
            "minutes": minutes,
            "sets": it.get("sets", None),
            "reps": it.get("reps", None),
            "note": str(it.get("note") or "")
        })

    return jsonify({
        "planTitle": plan_json.get("planTitle") or "AI Workout Plan",
        "totalMinutes": round(total_minutes, 1),
        "items": out_items
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
>>>>>>> 4775f5efed8b15f5daf7cf07ff3bc27f0d9015fd
