// docs/fileUtils.ts

interface FileIconInfo {
  icon: string; // En una implementación real, podría ser una ruta de imagen, un componente SVG, o una clase CSS
  color?: string; // Color opcional para el icono o texto
}

export const getFileIconDetails = (mimeType?: string, fileName?: string): FileIconInfo => {
  if (mimeType) {
    if (mimeType.startsWith("image/")) return { icon: "🖼️", color: "#3498db" }; // Icono de imagen genérico
    if (mimeType === "application/pdf") return { icon: "📄", color: "#e74c3c" }; // Icono de PDF
    if (mimeType.includes("word")) return { icon: "📝", color: "#2980b9" }; // DOC, DOCX
    if (mimeType.includes("excel") || mimeType.includes("spreadsheet")) return { icon: "📊", color: "#27ae60" }; // XLS, XLSX, CSV
    if (mimeType === "text/plain") return { icon: "📜", color: "#f39c12" }; // TXT
    if (mimeType === "text/csv") return { icon: "📊", color: "#27ae60" }; // CSV
  }

  if (fileName) {
    const ext = fileName.split('.').pop()?.toLowerCase();
    if (ext === "png" || ext === "jpg" || ext === "jpeg" || ext === "gif") return { icon: "🖼️", color: "#3498db" };
    if (ext === "pdf") return { icon: "📄", color: "#e74c3c" };
    if (ext === "doc" || ext === "docx") return { icon: "📝", color: "#2980b9" };
    if (ext === "xls" || ext === "xlsx") return { icon: "📊", color: "#27ae60" };
    if (ext === "csv") return { icon: "📊", color: "#27ae60" };
    if (ext === "txt") return { icon: "📜", color: "#f39c12" };
  }

  return { icon: "📁", color: "#95a5a6" }; // Icono genérico de archivo
};

// Función para obtener un nombre de clase CSS basado en el tipo de archivo (alternativa para usar con una librería de iconos)
export const getFileIconClass = (mimeType?: string, fileName?: string): string => {
  if (mimeType) {
    if (mimeType.startsWith("image/")) return "file-icon-image";
    if (mimeType === "application/pdf") return "file-icon-pdf";
    if (mimeType.includes("word")) return "file-icon-doc";
    if (mimeType.includes("excel") || mimeType.includes("spreadsheet")) return "file-icon-xls";
    if (mimeType === "text/plain") return "file-icon-txt";
    if (mimeType === "text/csv") return "file-icon-csv";
  }

  if (fileName) {
    const ext = fileName.split('.').pop()?.toLowerCase();
    if (ext === "png" || ext === "jpg" || ext === "jpeg" || ext === "gif") return "file-icon-image";
    if (ext === "pdf") return "file-icon-pdf";
    if (ext === "doc" || ext === "docx") return "file-icon-doc";
    if (ext === "xls" || ext === "xlsx") return "file-icon-xls";
    if (ext === "csv") return "file-icon-csv";
    if (ext === "txt") return "file-icon-txt";
  }

  return "file-icon-generic";
};
