"""Vulnerable: IDOR patterns — resource access by route ID without ownership check."""
from flask import Flask, jsonify, request

app = Flask(__name__)


@app.route("/api/documents/<doc_id>")
def get_document(doc_id):
    """Bad: fetches by ID with no ownership check."""
    doc = db.get_document(doc_id)
    return jsonify(doc)


@app.route("/api/invoices/<invoice_id>", methods=["GET"])
def get_invoice(invoice_id):
    """Bad: invoice access by ID, no user ownership check."""
    invoice = Invoice.query.get(invoice_id)
    return jsonify(invoice.to_dict())


@app.route("/api/orders/<order_id>/cancel", methods=["POST"])
def cancel_order(order_id):
    """Bad: destructive action on order by ID, no ownership verification."""
    order = Order.query.get(order_id)
    order.cancel()
    return jsonify({"cancelled": True})


# Bad: client-supplied price trusted directly
@app.route("/api/checkout", methods=["POST"])
def checkout():
    price = request.json.get("price")  # client-controlled price
    amount = request.json.get("balance")  # client-controlled balance
    payout = request.json.get("payout")  # client-controlled payout
    process_payment(price, amount)
    return jsonify({"charged": price})
