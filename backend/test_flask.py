from flask import Flask

app = Flask(__name__)

@app.route('/')
def index():
    return "¡Flask funciona!"

if __name__ == '__main__':
    print("Ejecutando test_flask.py")
    app.run(debug=True, port=5000)
