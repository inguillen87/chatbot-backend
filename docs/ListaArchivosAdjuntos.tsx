import React from 'react';
import { getFileIconDetails } from './fileUtils'; // Asegúrate que la ruta sea correcta
import { Button } from "@/components/ui/button"; // Asumiendo que usas Button para alguna acción

interface ArchivoInfo {
  name: string; // Nombre original del archivo
  mimeType?: string; // Tipo MIME
  url: string; // URL para descargar
  size?: number; // Tamaño en bytes
  // Podrías añadir más campos como id, fecha, etc.
}

interface ListaArchivosAdjuntosProps {
  archivos: ArchivoInfo[];
  onEliminarArchivo?: (fileName: string) => void; // Opcional: para eliminar un archivo
}

const ListaArchivosAdjuntos: React.FC<ListaArchivosAdjuntosProps> = ({ archivos, onEliminarArchivo }) => {
  if (!archivos || archivos.length === 0) {
    return <p className="text-sm text-gray-500">No hay archivos adjuntos.</p>;
  }

  const formatBytes = (bytes: number, decimals = 2) => {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const dm = decimals < 0 ? 0 : decimals;
    const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + ' ' + sizes[i];
  };

  return (
    <div className="space-y-2 mt-4">
      <h4 className="text-md font-semibold">Archivos Adjuntos:</h4>
      <ul className="list-none p-0">
        {archivos.map((archivo, index) => {
          const iconInfo = getFileIconDetails(archivo.mimeType, archivo.name);
          return (
            <li
              key={index}
              className="flex items-center justify-between p-2 border rounded-md hover:bg-gray-50"
            >
              <a
                href={archivo.url}
                target="_blank"
                rel="noopener noreferrer"
                download // Sugiere al navegador descargar el archivo
                className="flex items-center space-x-2 text-blue-600 hover:underline flex-grow min-w-0" // Añadido min-w-0 para que truncate funcione bien en flex
                title={`Descargar ${archivo.name}`}
              >
                {/* Usar iconInfo.color para aplicar color si es necesario, o definir clases específicas */}
                <span className="text-2xl" style={{ color: iconInfo.color }} role="img" aria-label={`Tipo: ${archivo.name.split('.').pop()}`}>
                  {iconInfo.icon}
                </span>
                <span className="truncate">{archivo.name}</span> {/* Quitado maxWidth fijo, dejar que flexbox y truncate manejen */}
                {archivo.size !== undefined && (
                  <span className="text-xs text-gray-500 ml-2 flex-shrink-0">({formatBytes(archivo.size)})</span> {/* flex-shrink-0 para que no se encoja */}
                )}
              </a>
              {onEliminarArchivo && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => onEliminarArchivo(archivo.name)} // O usar un ID si está disponible
                  className="text-red-500 hover:text-red-700"
                  title="Eliminar archivo"
                >
                  🗑️
                </Button>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
};

export default ListaArchivosAdjuntos;
