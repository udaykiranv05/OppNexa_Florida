
def match_keywords(item, keywords):
    searchable_text = " ".join(
        str(item.get(field, "") or "")
        for field in (
            "title",
            "description",
            "scope_summary",
            "agency",
            "commodity",
            "category",
        )
    ).lower()

    return [
        keyword
        for keyword in keywords
        if keyword.strip().lower() in searchable_text
    ]
