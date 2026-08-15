import React from 'react';import ReactDOM from 'react-dom/client';import {BrowserRouter} from 'react-router-dom';import App from './App';import './styles.css';import './trade-studio-theme.css';import './strategy-management.css';import './backtest-history.css';import './backtest-detail.css';
import './candle-interactions.css';
import './research.css';
import './settings.css';
ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><BrowserRouter><App/></BrowserRouter></React.StrictMode>)
