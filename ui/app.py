import streamlit as st
import requests

API_URL = "http://localhost:8000/query"

st.set_page_config(page_title="Supplier Price Lookup", page_icon="🔩", layout="centered")
st.title("Supplier Price Lookup")
st.caption("Gép-Coop procurement tool — fetches live prices and stock from supplier websites")

with st.form("query_form"):
    internal_part_no = st.text_input(
        "Internal Part No:",
        placeholder="e.g. 934128ZN",
        label_visibility="visible",
    )
    submitted = st.form_submit_button("Query", type="primary", use_container_width=True)

if submitted:
    if not internal_part_no.strip():
        st.warning("Please enter a part number.")
        st.stop()

    with st.spinner("Fetching price and stock from supplier website..."):
        try:
            resp = requests.post(
                API_URL,
                json={"internal_part_no": internal_part_no.strip()},
                timeout=90,
            )
            data = resp.json()
        except requests.exceptions.ConnectionError:
            st.error("Cannot connect to the backend. Is `uvicorn main:app --reload` running?")
            st.stop()
        except Exception as exc:
            st.error(f"Unexpected error: {exc}")
            st.stop()

    # ── Error from agent ──────────────────────────────────────────────────
    if "error" in data and not data.get("price_per_db"):
        st.error(data.get("message") or data.get("error"))
        st.stop()

    # ── Success: display results ──────────────────────────────────────────
    st.success("Result")

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("**Part numbers**")
        st.write(f"Internal:  `{data.get('internal_part_no', '')}`")
        st.write(f"Supplier:  `{data.get('supplier_part_no', '')}`")
        st.write(f"Supplier ID: `{data.get('supplier_id', '')}`")

    with col2:
        st.markdown("**Pricing**")
        price_per_db = data.get("price_per_db")
        price_raw = data.get("price_raw")
        price_unit_qty = data.get("price_unit_qty", 1)
        currency = data.get("currency", "HUF")
        unit = data.get("unit", "db")

        if price_per_db is not None:
            st.metric(
                label=f"Price per {unit} (normalised)",
                value=f"{price_per_db:.4f} {currency}/{unit}",
            )
            if price_unit_qty and price_unit_qty > 1:
                st.caption(
                    f"Original: {price_raw} {currency} / {price_unit_qty:,} {unit}"
                )

    st.divider()

    # ── Stock ─────────────────────────────────────────────────────────────
    st.markdown("**Stock**")
    stock = data.get("stock")

    if isinstance(stock, dict):
        sc1, sc2 = st.columns(2)
        sc1.metric("Budapest", f"{stock.get('budapest', 0):,} db")
        sc2.metric("Vecsés", f"{stock.get('vecsés', 0):,} db")
    elif stock is not None:
        st.metric("Stock", f"{stock:,} db")
    else:
        st.write("Stock data not available.")

    st.caption(f"Queried at: {data.get('queried_at', '')}")

    # ── Agent summary message ─────────────────────────────────────────────
    with st.expander("Agent summary"):
        st.write(data.get("message", ""))
