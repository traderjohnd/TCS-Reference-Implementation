const DECISION_STYLES = {
  Allow: 'bg-green-900/50 text-green-400 border-green-700',
  Observe: 'bg-blue-900/50 text-blue-400 border-blue-700',
  Hold: 'bg-yellow-900/50 text-yellow-400 border-yellow-700',
  Escalate: 'bg-orange-900/50 text-orange-400 border-orange-700',
  Stop: 'bg-red-900/50 text-red-400 border-red-700',
};

export default function StatusBadge({ decision }) {
  const style = DECISION_STYLES[decision] || 'bg-gray-800 text-gray-400 border-gray-600';
  return (
    <span className={`inline-flex px-2 py-0.5 rounded text-xs font-medium border ${style}`}>
      {decision}
    </span>
  );
}
