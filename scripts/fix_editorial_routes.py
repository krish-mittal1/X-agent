with open("app/api/editorial_routes.py", "r", encoding="utf-8") as f:
    text = f.read()

bad = """            "success": True,
            "status": REC_STATUS_APPLIED,
    REC_STATUS_REJECTED,
            "message": "Recommendation rejected by operator.","""

good = """            "success": True,
            "status": REC_STATUS_REJECTED,
            "message": "Recommendation rejected by operator.","""

text = text.replace(bad, good)

with open("app/api/editorial_routes.py", "w", encoding="utf-8") as f:
    f.write(text)
print("FIXED")
