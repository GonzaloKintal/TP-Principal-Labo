import { Routes, Route } from 'react-router-dom';
import AnalystDashboardIFrame from './AnalystDashboardIFrame';

const AnalystDashboard = () => {
  return (
    <div className="bg-background rounded-lg shadow p-6">
      <h1 className="text-2xl font-bold text-foreground">Panel de Analista</h1>
      <p className="mt-2 text-foreground">Reportes y datos estadísticos</p>
      
      <div className="mt-6">
        <Routes>
          <Route index element={<AnalystDashboardIFrame />} />
        </Routes>
      </div>
    </div>
  );
};

export default AnalystDashboard;