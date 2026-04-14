export default function MetricCard({ label, value, subtitle, color = 'blue' }) {
  const colors = {
    blue: 'border-blue-500 text-blue-400',
    green: 'border-green-500 text-green-400',
    yellow: 'border-yellow-500 text-yellow-400',
    red: 'border-red-500 text-red-400',
    purple: 'border-purple-500 text-purple-400',
  };

  return (
    <div className={`bg-gray-900 rounded-lg p-4 border-l-4 ${colors[color] || colors.blue}`}>
      <p className="text-xs text-gray-500 uppercase tracking-wider">{label}</p>
      <p className="text-2xl font-bold text-white mt-1">{value}</p>
      {subtitle && <p className="text-xs text-gray-500 mt-1">{subtitle}</p>}
    </div>
  );
}
