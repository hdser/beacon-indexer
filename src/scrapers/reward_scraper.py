from typing import Dict, List, Any, Optional

from src.scrapers.base_scraper import BaseScraper
from src.services.beacon_api_service import BeaconAPIService
from src.services.clickhouse_service import ClickHouseService
from src.utils.logger import logger

class RewardScraper(BaseScraper):
    """Scraper for rewards, using separate API endpoints."""
    
    def __init__(self, beacon_api: BeaconAPIService, clickhouse: ClickHouseService):
        super().__init__("reward_scraper", beacon_api, clickhouse)
    
    async def process(self, block_data: Dict) -> None:
        """Process a block and store reward information via API calls."""
        # Extract version and data
        version = block_data.get("version", "phase0")
        
        # Extract block information
        data = block_data.get("data", {})
        message = data.get("message", {})
        slot = int(message.get("slot", 0))
        
        # Get block root
        try:
            header_response = await self.beacon_api.get_block_header(str(slot))
            block_root = header_response.get("root", "")
        except Exception as e:
            logger.warning(f"Failed to get block header for slot {slot}: {e}")
            return
        
        # Process block rewards - requires a separate API call
        try:
            block_rewards = await self.beacon_api.get_block_rewards(str(slot))
            
            if block_rewards:
                # Insert block rewards
                query = """
                INSERT INTO block_rewards (
                    slot, block_root, proposer_index, total, attestations,
                    sync_aggregate, proposer_slashings, attester_slashings
                ) VALUES (
                    %(slot)s, %(block_root)s, %(proposer_index)s, %(total)s, %(attestations)s,
                    %(sync_aggregate)s, %(proposer_slashings)s, %(attester_slashings)s
                )
                """
                
                self.clickhouse.execute(query, {
                    "slot": slot,
                    "block_root": block_root,
                    "proposer_index": int(block_rewards.get("proposer_index", 0)),
                    "total": int(block_rewards.get("total", 0)),
                    "attestations": int(block_rewards.get("attestations", 0)),
                    "sync_aggregate": int(block_rewards.get("sync_aggregate", 0)),
                    "proposer_slashings": int(block_rewards.get("proposer_slashings", 0)),
                    "attester_slashings": int(block_rewards.get("attester_slashings", 0))
                })
                
                logger.info(f"Processed block rewards for slot {slot}")
        
        except Exception as e:
            logger.error(f"Error processing block rewards for slot {slot}: {e}")
        
        # Process sync committee rewards (post-phase0)
        if version in ["altair", "bellatrix", "capella", "deneb", "electra"]:
            try:
                sync_rewards_response = await self.beacon_api.get_sync_committee_rewards(str(slot))
                
                if sync_rewards_response:
                    # The response now contains data in the format:
                    # { "data": [...] }
                    sync_rewards = sync_rewards_response.get("data", [])
                    
                    # Prepare batch insert for sync committee rewards
                    query = """
                    INSERT INTO sync_committee_rewards (
                        slot, block_root, validator_index, reward
                    ) VALUES
                    """
                    
                    params = []
                    
                    for reward in sync_rewards:
                        params.append({
                            "slot": slot,
                            "block_root": block_root,
                            "validator_index": int(reward.get("validator_index", 0)),
                            "reward": int(reward.get("reward", 0))
                        })
                    
                    # Insert sync committee rewards
                    if params:
                        self.clickhouse.execute_many(query, params)
                        
                        logger.info(f"Processed {len(params)} sync committee rewards for block at slot {slot}")
            
            except Exception as e:
                logger.error(f"Error processing sync committee rewards for slot {slot}: {e}")
        
        # Process attestation rewards at epoch boundaries
        # Determine if this is an epoch boundary slot
        slots_per_epoch = self.get_slots_per_epoch()
        is_epoch_boundary = (slot + 1) % slots_per_epoch == 0
        
        if is_epoch_boundary:
            # Process attestation rewards for the current epoch
            epoch = slot // slots_per_epoch
            try:
                att_rewards_response = await self.beacon_api.get_attestation_rewards(epoch)
                
                if att_rewards_response:
                    # Handle the new API response format
                    execution_optimistic = att_rewards_response.get("execution_optimistic", False)
                    att_rewards_data = att_rewards_response.get("data", {})
                    
                    # Get rewards from the total_rewards array
                    total_rewards = att_rewards_data.get("total_rewards", [])
                    
                    # Prepare batch insert for attestation rewards
                    params = []
                    
                    for reward_data in total_rewards:
                        try:
                            validator_index = int(reward_data.get("validator_index", 0))
                            params.append({
                                "epoch": epoch,
                                "validator_index": validator_index,
                                "head": int(reward_data.get("head", 0)),
                                "target": int(reward_data.get("target", 0)),
                                "source": int(reward_data.get("source", 0)),
                                "inclusion_delay": int(reward_data.get("inclusion_delay", 0)) if reward_data.get("inclusion_delay") is not None else None,
                                "inactivity": int(reward_data.get("inactivity", 0))
                            })
                        except (ValueError, TypeError) as e:
                            logger.warning(f"Error processing reward data for epoch {epoch}: {e}, data: {reward_data}")
                    
                    # Insert attestation rewards in batches
                    if params:
                        query = """
                        INSERT INTO attestation_rewards (
                            epoch, validator_index, head, target, source, 
                            inclusion_delay, inactivity
                        ) VALUES
                        """
                        
                        # Use smaller batches for attestation rewards
                        batch_size = 500
                        for i in range(0, len(params), batch_size):
                            batch = params[i:i+batch_size]
                            self.clickhouse.execute_many(query, batch)
                        
                        logger.info(f"Processed {len(params)} attestation rewards for epoch {epoch}")
                
            except Exception as e:
                logger.error(f"Error processing attestation rewards for epoch {epoch}: {e}")
                import traceback
                logger.error(traceback.format_exc())