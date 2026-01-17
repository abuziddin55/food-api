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

# --------------------- WORKOUT PLAN (MULTI) ---------------------

WORKOUT_PROMPT = """
You are a professional fitness coach.

Create EXACTLY 4 DIFFERENT workout plans.

Rules:
- Return 4 plans, no more, no less
- Each plan must be different
- Use only exercises from the candidates list
- Each plan must contain 3 to 6 items
- minutes must be between 3 and 20
- Prefer a mix: strength + cardio + optional core
- Return STRICT JSON ONLY
- NO markdown
- NO explanation text

JSON SCHEMA:

{
  "plans": [
    {
      "planTitle": "string",
      "items": [
        {"id":"string","minutes":number,"sets":number|null,"reps":number|null,"note":"string"}
      ]
    }
  ]
}
"""

def safe_json_extract(text: str):
    t = (text or "").strip()
    try:
        return json.loads(t)
    except Exception:
        pass

    start = t.find("{")
    end = t.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(t[start:end + 1])

    raise ValueError("Gemini did not return valid JSON")

@app.post("/api/workout_plan")
def workout_plan():
    data = request.get_json(silent=True) or {}
    targetCalories = float(data.get("targetCalories", 0))
    weightKg = float(data.get("weightKg", 0))
    candidates = data.get("candidates", [])

    if targetCalories <= 0 or weightKg <= 0:
        return jsonify({"error": "targetCalories and weightKg must be > 0"}), 400

    if not candidates:
        return jsonify({"error": "candidates list is required"}), 400

    try:
        model = genai.GenerativeModel("gemini-2.0-flash")
        payload = {
            "targetCalories": targetCalories,
            "weightKg": weightKg,
            "candidates": candidates
        }

        result = model.generate_content(
            [WORKOUT_PROMPT, json.dumps(payload)]
        )

        raw = (result.text or "").strip()
        parsed = safe_json_extract(raw)
        plans = parsed.get("plans", [])

        if not isinstance(plans, list) or len(plans) != 4:
            raise ValueError("Expected exactly 4 plans")

    except Exception as e:
        return jsonify({
            "error": "Workout generation failed",
            "details": str(e)
        }), 500

    # 🔧 candidates lookup
    by_id = {c["id"]: c for c in candidates}

    out_plans = []

    for plan in plans:
        enriched_items = []
        total_minutes = 0.0

        for it in plan.get("items", []):
            ex_id = it.get("id")
            minutes = float(it.get("minutes", 0))

            if not ex_id or minutes <= 0 or ex_id not in by_id:
                continue

            cand = by_id[ex_id]
            minutes = max(3.0, min(20.0, minutes))
            total_minutes += minutes

            enriched_items.append({
                "id": ex_id,
                "name": cand.get("name"),
                "met": cand.get("met"),
                "minutes": minutes,
                "sets": it.get("sets"),
                "reps": it.get("reps"),
                "note": it.get("note", "")
            })

        if enriched_items:
            out_plans.append({
                "planTitle": plan.get("planTitle", "AI Workout Plan"),
                "totalMinutes": round(total_minutes, 1),
                "items": enriched_items
            })

    return jsonify({
        "plans": out_plans
    })
