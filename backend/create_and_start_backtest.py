#!/usr/bin/env python3
"""
创建并启动 BTCUSDT 1h 布林带均值回归策略回测
"""

import asyncio
import sys
from datetime import datetime
from sqlalchemy import select
from app.db import SessionLocal
from app.models import BacktestRun, ResearchProject, ResearchStatus, RunStatus, Strategy, StrategyVersion
from app.backtest_service import create_backtest_run, confirm_and_start_backtest
from app.schemas import BacktestCreate, BacktestConfirmRequest


async def main():
    strategy_slug = "btc_usdt_1h_bollinger_mean_reversion"
    
    async with SessionLocal() as db:
        # 查找策略
        strategy = await db.scalar(
            select(Strategy).where(Strategy.slug == strategy_slug)
        )
        if not strategy:
            print(f"Strategy {strategy_slug} not found")
            return 1
        
        # 获取最新版本
        version = await db.scalar(
            select(StrategyVersion)
            .where(StrategyVersion.strategy_id == strategy.id)
            .order_by(StrategyVersion.version.desc())
        )
        if not version:
            print(f"No version found for strategy {strategy_slug}")
            return 1
        
        print(f"Found strategy: {strategy.name} (ID: {strategy.id}) version {version.version}")
        
        # 创建研究项目
        project = ResearchProject(
            name="BTCUSDT 1h 布林带均值回归策略回测",
            description="使用Choppiness震荡过滤（chop_period=14, chop_threshold=0.4），不设额外止损，仅中轨平仓",
            status=ResearchStatus.BACKTESTING,
            strategy_id=strategy.id,
        )
        db.add(project)
        await db.flush()
        
        # 创建回测配置
        backtest_create = BacktestCreate(
            research_project_id=project.id,
            strategy_id=strategy.id,
            strategy_version_id=version.id,
            symbols=["BTCUSDT"],
            timeframes=["1h"],
            venue="BINANCE",
            start_date="2023-01-01",
            end_date="2026-08-01",
            initial_balance=10000,
            strategy_parameters={
                "bollinger_period": 20,
                "bollinger_std": 2.0,
                "chop_period": 14,
                "chop_threshold": 0.4,
                "position_pct": 0.1,
            },
            description="BTCUSDT 1小时级别布林带均值回归反转策略回测",
        )
        
        # 创建回测运行
        run_id = await create_backtest_run(backtest_create, db)
        print(f"Created backtest run: {run_id}")
        
        # 确认并启动回测（跳过数据检查，我们已经确认数据存在）
        await confirm_and_start_backtest(
            BacktestConfirmRequest(
                run_id=run_id,
                confirm_missing=False
            ),
            db
        )
        
        print(f"Backtest started successfully! Run ID: {run_id}")
        await db.commit()
        
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
