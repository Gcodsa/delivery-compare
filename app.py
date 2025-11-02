from flask import Flask, render_template, request

app = Flask(__name__)

# وجبات تجريبية (بدل الزحف الحقيقي)
MOCK_MENUS = {
    "هرفي": [
        {"name": "برجر دجاج", "price": 18.5, "image": "https://i.imgur.com/XAn8M1t.png"},
        {"name": "برجر لحم", "price": 21.0, "image": "https://i.imgur.com/DyUWhV5.png"},
        {"name": "وجبة مايونيز", "price": 19.5, "image": "https://i.imgur.com/WC2iX3S.png"},
    ],
    "ماكدونالدز": [
        {"name": "بيج ماك", "price": 24.0, "image": "https://i.imgur.com/Lj0E8I7.png"},
        {"name": "ماك تشيكن", "price": 21.5, "image": "https://i.imgur.com/H4BdYMj.png"},
    ],
}

# بيانات مقارنة تجريبية
MOCK_COMPARISON = {
    "برجر دجاج": [
        {"app": "هنقرستيشن", "item_price": 18.5, "delivery_fee": 6, "total": 24.5},
        {"app": "جاهز", "item_price": 18.0, "delivery_fee": 5, "total": 23.0},
        {"app": "كيتا", "item_price": 17.8, "delivery_fee": 5.5, "total": 23.3},
        {"app": "تو يو", "item_price": 18.9, "delivery_fee": 4.5, "total": 23.4},
        {"app": "مستر مندوب", "item_price": 19.0, "delivery_fee": 5, "total": 24.0},
    ],
    "برجر لحم": [
        {"app": "هنقرستيشن", "item_price": 21.0, "delivery_fee": 6, "total": 27.0},
        {"app": "جاهز", "item_price": 20.5, "delivery_fee": 5, "total": 25.5},
        {"app": "كيتا", "item_price": 20.8, "delivery_fee": 5.5, "total": 26.3},
        {"app": "تو يو", "item_price": 21.2, "delivery_fee": 4.5, "total": 25.7},
        {"app": "مستر مندوب", "item_price": 21.5, "delivery_fee": 5, "total": 26.5},
    ],
}

@app.route("/", methods=["GET", "POST"])
def index():
    query = request.form.get("restaurant", "").strip()
    menu = MOCK_MENUS.get(query)
    return render_template("index.html", query={"restaurant": query}, menu=menu)

@app.route("/compare", methods=["POST"])
def compare():
    restaurant = request.form.get("restaurant", "")
    meal_name = request.form.get("meal_name", "")
    results = MOCK_COMPARISON.get(meal_name, [])
    results = sorted(results, key=lambda x: x["total"])  # الأرخص أولاً
    return render_template("index.html", query={"restaurant": restaurant}, menu=None, results=results, meal_name=meal_name)

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
