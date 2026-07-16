import json
from typing import List, Dict, Any, TypedDict

# Tipos para los datos de los documentos
class DocumentoTexto(TypedDict):
    tipo: str # "texto"
    contenido_texto: str
    nombre_original: str

class DocumentoImagen(TypedDict):
    tipo: str # "imagen"
    data_uri: str # base64 data URI
    nombre_original: str

class DocumentoCSV(TypedDict):
    tipo: str # "csv"
    contenido_texto: str # Contenido del CSV como string
    nombre_original: str

class DocumentoJSON(TypedDict):
    tipo: str # "json_data"
    contenido_json: Dict[str, Any] # Contenido del JSON parseado
    nombre_original: str

DocumentoEntrada = DocumentoTexto | DocumentoImagen | DocumentoCSV | DocumentoJSON

def construir_prompt_multimodal(documentos: List[DocumentoEntrada], contexto_cliente: str) -> str:
    prompt_inicio = f"""Eres un equipo de analistas de datos de élite y consultores de negocios de nivel mundial, similar a McKinsey o Boston Consulting Group. Tu cliente es "{contexto_cliente}". Tu tarea es realizar un análisis multimodal profundo de TODOS los documentos proporcionados y entregar un informe de inteligencia accionable de la más alta calidad. Tu respuesta DEBE ser completamente en español y seguir estrictamente la estructura JSON especificada.

**Documentos a Analizar:**
"""
    documentos_prompt_parts = []
    for i, doc in enumerate(documentos):
        nombre_doc = doc.get('nombre_original', f'documento_{i+1}')
        if doc['tipo'] == 'texto':
            documentos_prompt_parts.append(f"""
**Documento {i+1} (Texto - {nombre_doc}):**
```
{doc['contenido_texto']}
```
""")
        elif doc['tipo'] == 'imagen':
            documentos_prompt_parts.append(f"""
**Documento {i+1} (Imagen - {nombre_doc}):**
{{{{media url="{doc['data_uri']}"}}}}
""")
        elif doc['tipo'] == 'csv':
            documentos_prompt_parts.append(f"""
**Documento {i+1} (CSV - {nombre_doc}):**
```
{doc['contenido_texto']}
```
(Nota: Este es el contenido de un archivo CSV. Analiza sus datos, busca patrones y correlaciones con otros documentos.)
""")
        elif doc['tipo'] == 'json_data':
            try:
                json_str = json.dumps(doc['contenido_json'], indent=2, ensure_ascii=False)
            except TypeError:
                json_str = str(doc['contenido_json']) # Fallback por si no es serializable directamente
            documentos_prompt_parts.append(f"""
**Documento {i+1} (Datos JSON - {nombre_doc}):**
```json
{json_str}
```
(Nota: Este es el contenido de un archivo JSON. Analiza sus datos y relaciones con otros documentos.)
""")

    prompt_documentos = "".join(documentos_prompt_parts)

    prompt_instrucciones_y_formato = """
**Instrucciones de Análisis Clave:**
1.  **Análisis Integrado y Referencias:** Tu análisis debe ser sintético, correlacionando la información de TODOS los documentos proporcionados (identificados por su nombre, ej., "Documento 1 (Texto - Informe_Q1.txt)"). Al discutir hallazgos o datos, **haz referencia explícita al documento de origen** (ej., "Según el Documento 1 (Informe_Q1.txt)...", "La imagen (Documento 2 - Grafico_Ventas.png) muestra..."). No analices los documentos de forma aislada. Busca explícitamente conexiones, contradicciones o corroboraciones entre diferentes fuentes de datos. Si un documento parece irrelevante para un tema particular, puedes omitir su mención en ese tema específico, pero considera si su irrelevancia es en sí misma un hallazgo. Si encuentras contradicciones directas, señálalas claramente.
2.  **Búsqueda de Insights Accionables:** Busca patrones, tendencias, correlaciones, anomalías y relaciones significativas entre los datos presentados en los diferentes documentos. Prioriza la identificación de los hallazgos más críticos y que conduzcan a recomendaciones accionables.
3.  **Análisis Cuantitativo y Sugerencias de Gráficos:**
    a.  Si encuentras datos cuantitativos comparativos o distributivos en los textos, CSVs o JSONs que se presten a visualización y aporten un valor claro al análisis, puedes sugerir un gráfico.
    b.  En la sección "Analisis Tematico Profundo" (`keyThemes`), bajo el tema correspondiente, si un gráfico es pertinente para ilustrar tus hallazgos, incluye una sub-sección `suggestedChart`.
    c.  Dentro de `suggestedChart`, especifica:
        *   `chartType`: String, debe ser "bar" (para comparaciones entre categorías o series temporales cortas) o "pie" (para mostrar partes de un todo, usar con moderación, ideal para pocas categorías). Asegúrate de que el tipo de gráfico sea el más adecuado para los datos.
        *   `chartTitle`: String, un título descriptivo y conciso para el gráfico.
        *   `chartData`: Array de objetos. Para "bar", cada objeto debe tener `label` (String) y `value` (Number, no string). Para "pie", cada objeto debe tener `label` (String) y `value` (Number, no string). **Es crucial que los `value` sean estrictamente numéricos.**
        *   `chartDescription`: String, una breve explicación de lo que el gráfico muestra, qué insight clave revela y cómo se relaciona con los documentos analizados.
    d.  No intentes generar la imagen del gráfico, solo proporciona los datos estructurados.
    e.  **Priorización:** Sugiere gráficos solo si clarifican significativamente un punto clave del análisis. Limita las sugerencias a un máximo de 1-2 gráficos bien justificados por informe, a menos que múltiples gráficos distintos sean excepcionalmente relevantes para diferentes temas clave. Evita gráficos triviales.
4.  **Nombres de Temas (`topicName`):** El `topicName` en `keyThemes` debe ser específico y reflejar el análisis multimodal si aplica (ej., 'Correlación de Gasto en Marketing (Doc A) con Ventas por Producto (Doc B y C)').

**Formato del Informe (Sigue esta estructura JSON estrictamente. Tu respuesta DEBE ser ÚNICAMENTE el objeto JSON válido, sin ningún texto, explicación, saludo o comentario antes o después del bloque JSON. Comienza tu respuesta directamente con `{` y termínala con `}`.):**
```json
{
  "executiveSummary": "Resumen ejecutivo conciso (máximo 200 palabras) destacando 2-3 hallazgos críticos y recomendaciones clave del análisis multimodal, haciendo referencia a los insights derivados de la combinación de los documentos.",
  "keyThemes": [
    {
      "topicName": "Nombre claro y específico del tema que refleje el análisis multimodal si aplica (ej: 'Impacto de la campaña X (Doc A) en las menciones en redes (Doc B) y ventas (Doc C)')",
      "summary": "Análisis detallado integrando información de múltiples documentos. Mencionar explícitamente cómo se relacionan los documentos (ej: 'El Documento A (Informe Anual) indica una inversión de $Y en la campaña X. Paralelamente, el Documento B (CSV de Redes Sociales) muestra un aumento del Z% en menciones positivas durante el periodo de la campaña. La imagen (Documento C - Gráfico de Ventas) también sugiere un repunte en ventas del producto promocionado, aunque se necesita análisis adicional para confirmar causalidad directa.'). Si hay contradicciones, explicarlas.",
      "sentiment": "Etiqueta de sentimiento (Positivo, Negativo, Mixto)",
      "sentimentScore": 0.8,
      "keyDataPoints": [
        "Dato o cita clave 1 (ej: 'Documento A (Informe Anual): Inversión campaña X fue $Y')",
        "Dato o cita clave 2 (ej: 'Documento B (CSV Redes): Menciones positivas +Z%')"
      ],
      "wordCloudKeywords": ["palabra_clave_multimodal", "campaña_X", "ventas_producto_Y", "feedback_redes"],
      "suggestedChart": { // Este campo es opcional. Incluir solo si se cumplen los criterios de las instrucciones.
        "chartType": "bar", // "bar" o "pie"
        "chartTitle": "Ej: Correlación Inversión Marketing vs. Menciones Positivas",
        "chartData": [ // Asegurar que 'value' sea numérico.
          {"label": "Inversión ($)", "value": 5000},
          {"label": "Menciones Post-Campaña", "value": 1500}
        ],
        "chartDescription": "El gráfico ilustra la relación entre la inversión en la campaña X (fuente: Doc A) y el subsiguiente aumento de menciones positivas (fuente: Doc B)."
      }
    }
  ],
  "strategicRecommendations": [
    {
      "title": "Título claro y accionable de la recomendación basada en el análisis integrado",
      "recommendation": "Explicación detallada del 'qué', 'porqué' y 'cómo', fundamentada en los hallazgos de los múltiples documentos.",
      "priority": "Crítica | Alta | Media | Baja",
      "departmentsInvolved": ["Finanzas", "Marketing"],
      "kpi": "Indicador medible (ej: 'Incrementar ROI de campañas en 10% en 6 meses')",
      "potentialRisks": "Desafíos en la implementación.",
      "supportingDocuments": ["Nombre Documento A (ej: Informe Anual)", "Nombre Documento B (ej: CSV Redes)"] // Listar nombres de los documentos que PRINCIPALMENTE respaldan esta recomendación.
    }
  ]
}
```

**Tono y Estilo:** Tu lenguaje debe ser profesional, basado en datos y autoritario. El objetivo es impresionar al cliente con la profundidad y la claridad de tu análisis, dándole un plan de acción concreto y de alto valor derivado de la síntesis de todos los documentos. Recuerda, la respuesta final debe ser únicamente el objeto JSON. Verifica que el JSON sea válido antes de responder.
"""
    return prompt_inicio + prompt_documentos + prompt_instrucciones_y_formato

# Ejemplo de cómo se podría llamar (esto iría en la lógica del servicio)
if __name__ == '__main__':
    mock_documentos = [
        DocumentoTexto(tipo="texto", nombre_original="Informe_Q1.txt", contenido_texto="Las ventas en Q1 fueron de $100,000. El producto A fue el más vendido."),
        DocumentoImagen(tipo="imagen", nombre_original="Grafico_Ventas.png", data_uri="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAUA..."),
        DocumentoCSV(tipo="csv", nombre_original="datos_ventas_detalle.csv", contenido_texto="producto,cantidad,precio\nProducto A,50,10\nProducto B,30,20"),
        DocumentoJSON(tipo="json_data", nombre_original="feedback_clientes.json", contenido_json={"cliente1": {"rating": 5, "comment": "Excelente producto A"}, "cliente2": {"rating": 3, "comment": "Producto B regular"}})
    ]
    prompt_generado = construir_prompt_multimodal(mock_documentos, "Mi Empresa S.A.")
    print(prompt_generado)

    # Simulación de la estructura JSON esperada para validación visual
    expected_json_structure_example = {
      "executiveSummary": "...",
      "keyThemes": [
        {
          "topicName": "...",
          "summary": "...",
          "sentiment": "Positivo",
          "sentimentScore": 0.9,
          "keyDataPoints": ["Doc A: Dato X", "Doc B: Dato Y"],
          "wordCloudKeywords": ["keyword1", "keyword2"],
          "suggestedChart": { # Opcional, puede no estar si no hay gráfico
            "chartType": "bar", # "bar" o "pie"
            "chartTitle": "Título del Gráfico",
            "chartData": [
              {"label": "Categoría 1", "value": 100},
              {"label": "Categoría 2", "value": 200}
            ],
            "chartDescription": "Descripción del insight del gráfico."
          }
        }
      ],
      "strategicRecommendations": [
        {
          "title": "...",
          "recommendation": "...",
          "priority": "Alta",
          "departmentsInvolved": ["Marketing"],
          "kpi": "...",
          "potentialRisks": "...",
          "supportingDocuments": ["Informe_Q1.txt", "datos_ventas_detalle.csv"]
        }
      ]
    }
    print("\n--- Ejemplo de Estructura JSON Esperada (para referencia) ---")
    print(json.dumps(expected_json_structure_example, indent=2, ensure_ascii=False))
