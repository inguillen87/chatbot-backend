from flask import Blueprint, request, jsonify
from twilio.twiml.messaging_response import MessagingResponse  # type: ignore
from services.faq_matcher_spacy import buscar_en_faq_spacy
from services.nlp import get_gpt_response
from models import QA, User
from extensions import db
from datetime import datetime
import logging
import os
from services.logic import responder_chatboc

chat_bp = Blueprint('chat', __name__)

# ✅ Ruta principal para usuarios logueados
@chat_bp.route('/ask', methods=['POST'])
def ask():
      return responder_chatboc()
      