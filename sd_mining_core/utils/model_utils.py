import os
import sys
import torch
import io
import gc
import logging
import time
from diffusers import AutoencoderKL, DPMSolverMultistepScheduler
from vendor.lpw_stable_diffusion_xl import StableDiffusionXLLongPromptWeightingPipeline
from vendor.lpw_stable_diffusion import StableDiffusionLongPromptWeightingPipeline

def get_local_model_ids(config):
    local_files = os.listdir(config.base_dir)
    local_model_ids = []
    
    for model in config.model_configs.values():
        model_id = model['name']
        if 'base' in model:
            base_file = model['base'] + ".safetensors"
            name_file = model['name'] + ".safetensors"
            if base_file in local_files and name_file in local_files:
                local_model_ids.append(model_id)
            else:
                if base_file not in local_files:
                    logging.warning(f"Base model file '{model['base']}' not found for model '{model['name']}'.")
                if name_file not in local_files:
                    logging.warning(f"LoRA weights file '{model['name']}' not found for model '{model['name']}'.")
        else:
            if model_id + ".safetensors" in local_files:
                local_model_ids.append(model_id)
            else:
                logging.warning(f"Model file for '{model['name']}' not found in local directory.")
    
    return local_model_ids

def load_model(config, model_id):
    start_time = time.time()

    # model_id may be a LoRa name. If so, we need to find the base model ID from models.json
    lora_config = config.lora_configs.get(model_id)
    model_config = config.model_configs.get(model_id)

    if lora_config is not None and model_config is not None:
        composite_model_config = model_config
        if 'base' not in composite_model_config:
            raise ValueError(f"Model configuration for {model_id} is missing the 'base' field.")
        base_model_id = composite_model_config['base']
    else:
        composite_model_config = None
        base_model_id = model_id

    base_model_config = config.model_configs.get(base_model_id)
    if base_model_config is None:
        raise ValueError(f"Model configuration for {base_model_id} not found.")

    if config.exclude_sdxl and base_model_id.startswith("SDXL"):
        raise ValueError(f"Loading of 'sdxl' models is disabled. Model '{base_model_id}' cannot be loaded as per configuration.")

    base_model_file_path = os.path.join(config.base_dir, f"{base_model_id}.safetensors")

    if base_model_config['type'] == "sd15":
        pipe = StableDiffusionLongPromptWeightingPipeline.from_single_file(
            base_model_file_path, torch_dtype=torch.float16
        ).to('cuda:' + str(config.cuda_device_id))
        pipe.scheduler = DPMSolverMultistepScheduler.from_config(
            pipe.scheduler.config, use_karras_sigmas=True, algorithm_type="sde-dpmsolver++"
        )
    else:
        pipe = StableDiffusionXLLongPromptWeightingPipeline.from_single_file(
            base_model_file_path, torch_dtype=torch.float16
        ).to('cuda:' + str(config.cuda_device_id))

    pipe.safety_checker = None

    if 'vae' in base_model_config:
        vae_name = base_model_config['vae']
        vae_file_path = os.path.join(config.base_dir, f"{vae_name}.safetensors")
        vae = AutoencoderKL.from_single_file(
            vae_file_path, torch_dtype=torch.float16
        ).to('cuda:' + str(config.cuda_device_id))
        pipe.vae = vae

    if composite_model_config is not None:
        pipe = load_lora_weights(config, pipe, base_model_config['type'], model_id)

    end_time = time.time()
    loading_latency = end_time - start_time

    return pipe, loading_latency

def load_lora_weights(config, pipe, base_model_type, lora_id):
    lora_config = config.lora_configs.get(lora_id)

    if lora_config is None:
        raise ValueError(f"LoRa ID '{lora_id}' not found in configuration.")
    if lora_config['base_model'] != base_model_type:
        raise ValueError(f"LoRa '{lora_id}' is not compatible with the loaded model type '{base_model_type}'.")
    
    lora_file_path = os.path.join(config.base_dir, f"{lora_id}.safetensors")
    if not os.path.exists(lora_file_path):
        raise FileNotFoundError(f"LoRa weights file '{lora_file_path}' not found.")
    
    try:
        pipe.load_lora_weights(lora_file_path)
        config.loaded_loras[lora_id] = pipe
        return pipe
    except Exception as e:
        raise ValueError(f"Failed to load LoRa weights for '{lora_id}': {e}")

def unload_model(config, model_id):
    if model_id in config.loaded_models:
        del config.loaded_models[model_id]
        torch.cuda.empty_cache()
        gc.collect()

def unload_lora_weights(config, pipe, lora_id):
    if lora_id in config.loaded_loras:
        del config.loaded_loras[lora_id]
        pipe.unload_lora_weights()
        torch.cuda.empty_cache()
        gc.collect()

def load_default_model(config):
    model_ids = get_local_model_ids(config)
    if not model_ids:
        logging.error("No local models found. Exiting...")
        sys.exit(1)  # Exit if no models are available locally

    default_model_id = model_ids[config.default_model_id] if config.default_model_id < len(model_ids) else model_ids[0]
    base_model_id = config.model_configs[default_model_id]['base'] if 'base' in config.model_configs[default_model_id] else default_model_id

    if base_model_id not in config.loaded_models:
        current_model, _ = load_model(config, default_model_id)
        config.loaded_models[base_model_id] = current_model
        if base_model_id != default_model_id:
            logging.info(f"Default model {default_model_id} (base: {base_model_id}) loaded successfully.")
        else:
            logging.info(f"Default model {default_model_id} loaded successfully.")

def reload_model(config, model_id_from_signal):
    if config.loaded_models:
        model_to_unload = next(iter(config.loaded_models))
        # Check if the model has associated LoRa weights
        for lora_id, loaded_pipe in config.loaded_loras.items():
            if loaded_pipe == config.loaded_models[model_to_unload]:
                unload_lora_weights(config, loaded_pipe, lora_id)
                logging.info(f"Unloaded LoRa weights {lora_id} associated with model {model_to_unload}")
                break
        unload_model(config, model_to_unload)
        logging.info(f"Unloaded model {model_to_unload} to make space for {model_id_from_signal}")

    current_model, _ = load_model(config, model_id_from_signal)
    base_model_id_from_signal = config.model_configs[model_id_from_signal]['base'] if 'base' in config.model_configs[model_id_from_signal] else model_id_from_signal
    config.loaded_models[base_model_id_from_signal] = current_model
    if base_model_id_from_signal != model_id_from_signal:
        logging.info(f"Received model {model_id_from_signal} (base: {base_model_id_from_signal}) loaded successfully.")
    else:
        logging.info(f"Received model {model_id_from_signal} loaded successfully.")

def execute_model(config, model_id, prompt, neg_prompt, height, width, num_iterations, guidance_scale, seed):
    try:
        current_model = config.loaded_models.get(model_id) or config.loaded_loras.get(model_id)
        if current_model is None:
            raise ValueError(f"Model '{model_id}' not found in loaded models or loaded LoRAs.")

        model_config = config.model_configs.get(model_id, {})
        loading_latency = None  # Indicates no loading occurred if the model was already loaded

        kwargs = {
            # For better/stable image quality, consider using larger height x weight values
            'height': min(height - height % 8, config.config['processing_limits']['max_height']),
            'width': min(width - width % 8, config.config['processing_limits']['max_width']),
            'num_inference_steps': min(num_iterations, config.config['processing_limits']['max_iterations']),
            'guidance_scale': guidance_scale,
            'negative_prompt': neg_prompt,
        }

        if current_model == config.loaded_loras.get(model_id):
            default_weight = model_config.get('default_weight')
            if default_weight is not None:
                kwargs['cross_attention_kwargs'] = {"scale": default_weight}

        if seed is not None and seed >= 0:
            kwargs['generator'] = torch.Generator().manual_seed(seed)

        logging.debug(f"Executing model {model_id} with parameters: {kwargs}")

        inference_start_time = time.time()
        images = current_model(prompt, **kwargs).images
        inference_end_time = time.time()
        inference_latency = inference_end_time - inference_start_time

        image_data = io.BytesIO()
        images[0].save(image_data, format='PNG')
        image_data.seek(0)

        return image_data, inference_latency, loading_latency
    
    except Exception as e:
        logging.error(f"Error executing model {model_id}: {e}")
        raise