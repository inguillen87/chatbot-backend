import React from 'react';
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  BarElement,
  Title,
  Tooltip,
  Legend,
  ArcElement,
} from 'chart.js';
import { Bar, Pie } from 'react-chartjs-2';

ChartJS.register(
  CategoryScale,
  LinearScale,
  BarElement,
  ArcElement,
  Title,
  Tooltip,
  Legend
);

interface ChartDataPoint {
  label: string;
  value: number;
}

interface SuggestedChartProps {
  chartType: 'bar' | 'pie';
  chartTitle: string;
  chartData: ChartDataPoint[];
  chartDescription?: string;
}

const GeneradorGrafico: React.FC<SuggestedChartProps> = ({
  chartType,
  chartTitle,
  chartData,
  chartDescription,
}) => {
  const labels = chartData.map(d => d.label);
  const dataValues = chartData.map(d => d.value);

  const commonOptions = {
    responsive: true,
    plugins: {
      legend: {
        position: 'top' as const,
      },
      title: {
        display: true,
        text: chartTitle,
        font: {
          size: 16,
        }
      },
      tooltip: {
        callbacks: {
          label: function(context: any) {
            let label = context.dataset.label || '';
            if (label) {
              label += ': ';
            }
            if (context.parsed.y !== null && chartType === 'bar') {
              label += new Intl.NumberFormat('es-ES').format(context.parsed.y);
            }
            if (context.parsed !== null && chartType === 'pie') {
                 label = context.label + ': ' + new Intl.NumberFormat('es-ES').format(context.parsed);
            }
            return label;
          }
        }
      }
    },
  };

  const barChartData = {
    labels,
    datasets: [
      {
        label: chartTitle, // O podrías tener un label más genérico como 'Valor'
        data: dataValues,
        backgroundColor: [ // Colores de ejemplo, se pueden personalizar más
          'rgba(54, 162, 235, 0.6)',
          'rgba(255, 99, 132, 0.6)',
          'rgba(75, 192, 192, 0.6)',
          'rgba(255, 206, 86, 0.6)',
          'rgba(153, 102, 255, 0.6)',
          'rgba(255, 159, 64, 0.6)',
        ],
        borderColor: [
          'rgba(54, 162, 235, 1)',
          'rgba(255, 99, 132, 1)',
          'rgba(75, 192, 192, 1)',
          'rgba(255, 206, 86, 1)',
          'rgba(153, 102, 255, 1)',
          'rgba(255, 159, 64, 1)',
        ],
        borderWidth: 1,
      },
    ],
  };

  const pieChartData = {
    labels,
    datasets: [
      {
        label: chartTitle, // O 'Valores'
        data: dataValues,
        backgroundColor: [ // Colores de ejemplo
          'rgba(255, 99, 132, 0.7)',
          'rgba(54, 162, 235, 0.7)',
          'rgba(255, 206, 86, 0.7)',
          'rgba(75, 192, 192, 0.7)',
          'rgba(153, 102, 255, 0.7)',
          'rgba(255, 159, 64, 0.7)',
        ],
        borderColor: [
          'rgba(255, 99, 132, 1)',
          'rgba(54, 162, 235, 1)',
          'rgba(255, 206, 86, 1)',
          'rgba(75, 192, 192, 1)',
          'rgba(153, 102, 255, 1)',
          'rgba(255, 159, 64, 1)',
        ],
        borderWidth: 1,
      },
    ],
  };

  return (
    <div style={{ marginTop: '20px', marginBottom: '20px', padding: '10px', border: '1px solid #eee', borderRadius: '5px' }}>
      {chartType === 'bar' && <Bar options={commonOptions} data={barChartData} />}
      {chartType === 'pie' && <Pie options={commonOptions} data={pieChartData} />}
      {chartDescription && (
        <p style={{ textAlign: 'center', marginTop: '10px', fontSize: '0.9em', color: '#555' }}>
          {chartDescription}
        </p>
      )}
    </div>
  );
};

export default GeneradorGrafico;
