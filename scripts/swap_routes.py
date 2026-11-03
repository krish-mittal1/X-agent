with open("app/api/editorial_routes.py", "r", encoding="utf-8") as f:
    text = f.read()

target_fn1_start = '@router.get("/feedback/{draft_id}")'
target_fn2_start = '@router.get("/feedback/summary")'

idx1 = text.find(target_fn1_start)
idx2 = text.find(target_fn2_start)

if idx1 != -1 and idx2 != -1 and idx1 < idx2:
    idx3 = text.find('@router.get("/learning/recommendations")')
    summary_block = text[idx2:idx3]
    history_block = text[idx1:idx2]
    text = text[:idx1] + summary_block + history_block + text[idx3:]
    with open("app/api/editorial_routes.py", "w", encoding="utf-8") as f:
        f.write(text)
    print("SWAPPED ROUTE ORDER")
else:
    print("COULD NOT SWAP", idx1, idx2)
