class OrderEvent(db.Model, TimestampMixin):
    __tablename__ = "order_event"

    id = db.Column(db.Integer, primary_key=True)
    pyme_pedido_id = db.Column(db.Integer, db.ForeignKey("pyme_pedido.id"), nullable=True, index=True)
    market_order_id = db.Column(db.Integer, db.ForeignKey("market_order.id"), nullable=True, index=True)
    type = db.Column(db.String(50), nullable=False) # 'created', 'notification_sent', 'status_changed', 'error'
    payload = db.Column(JSONType, nullable=True)

    pyme_pedido = db.relationship("PymePedido", backref=db.backref("events", lazy="dynamic"))
    market_order = db.relationship("MarketOrder", backref=db.backref("events", lazy="dynamic"))

    def __repr__(self):
        return f"<OrderEvent type={self.type} pyme={self.pyme_pedido_id} market={self.market_order_id}>"
