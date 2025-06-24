class DecodeError(Exception):
    pass

class _Encoder:
    def encode(self, obj):
        import json
        return json.dumps(obj).encode('utf-8')

class _Decoder:
    def decode(self, data):
        import json
        if isinstance(data, (bytes, bytearray)):
            data = data.decode('utf-8')
        return json.loads(data)

class msgpack:
    Encoder = _Encoder
    Decoder = _Decoder

class json:
    Encoder = _Encoder
    Decoder = _Decoder
